"""Offline, fail-closed construction of Capri images from a verified payload.

This module never opens a device or invokes a flashing utility.  Publication is
atomic: the requested output directory must not already exist.

Requires host avbtool, debugfs and e2fsck. A host fec on PATH takes precedence
over the bundled Android executable. On Linux, install native fec when that
Android executable needs an unavailable /system/bin/linker; qemu alone does
not supply the Android runtime. FEC generation is never silently disabled.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile

from .modules import ModuleError, _parse


class ImageError(Exception):
    """An input cannot safely be transformed into a complete image set."""


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(args, *, cwd=None, env=None, codes=(0,)):
    try:
        result = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
                                capture_output=True)
    except OSError as exc:
        raise ImageError(f"Cannot execute {args[0]}: {exc}") from exc
    if result.returncode not in codes:
        raise ImageError(f"{args[0]} failed ({result.returncode}): "
                         + result.stderr.decode(errors="replace"))
    return result


def _safe_name(name):
    p = PurePosixPath(name)
    return (bool(name) and not p.is_absolute() and str(p) == name
            and not any(x in ("", ".", "..") for x in name.split("/"))
            and not any(c in name for c in "\\\x00\r\n\""))


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImageError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _extract(payload, destination):
    """Validate the exhaustive manifest before writing any archive member."""
    try:
        with zipfile.ZipFile(payload) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            if len(set(names)) != len(names) or "payload.json" not in names:
                raise ImageError("Payload has duplicate entries or no manifest")
            for entry in entries:
                mode = entry.external_attr >> 16
                if (not _safe_name(entry.filename) or entry.is_dir()
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or entry.flag_bits & 1):
                    raise ImageError(f"Unsafe payload entry: {entry.filename}")
            metadata = json.loads(archive.read("payload.json"), object_pairs_hook=_unique_json)
            if (not isinstance(metadata, dict)
                    or set(metadata) != {"format", "device", "kernel_release", "files"}
                    or metadata["format"] != "kernelforge-capri-payload-v1"
                    or metadata["device"] != "capri"
                    or not isinstance(metadata["kernel_release"], str)
                    or not re.fullmatch(r"[A-Za-z0-9._+-]+", metadata["kernel_release"])
                    or not isinstance(metadata["files"], dict)):
                raise ImageError("Unsupported payload manifest")
            files = metadata["files"]
            if set(files) != set(names) - {"payload.json"}:
                raise ImageError("Manifest does not exhaustively describe the payload")
            required = {"boot-files/Image", "tools/magiskboot", "tools/fec",
                        "module-files/early.sha256",
                        "module-files/vendor/lib/modules/modules.sha256"}
            if not required <= files.keys():
                raise ImageError("Payload is incomplete")
            for name, digest in files.items():
                allowed = (name in required or re.fullmatch(
                    r"(?:module-files/vendor/lib/modules|vendor_boot-files/ramdisk/lib/modules)/[A-Za-z0-9_.+-]+", name))
                if not allowed or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ImageError(f"Unsupported payload file: {name}")
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                if _sha(target) != digest:
                    raise ImageError(f"Payload checksum mismatch: {name}")
            for tool in ("magiskboot", "fec"):
                (destination / "tools" / tool).chmod(0o755)
            return metadata
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError) as exc:
        raise ImageError(f"Invalid payload: {exc}") from exc


def _tool(path):
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[5] != 1:
        raise ImageError(f"Expected little-endian ELF tool: {path}")
    machine = int.from_bytes(header[18:20], "little")
    host = platform.machine().lower()
    native = ((machine == 40 and host in ("arm", "armv7l", "armv8l"))
              or (machine == 183 and host in ("aarch64", "arm64"))
              or (machine == 62 and host in ("x86_64", "amd64")))
    if native:
        return [str(path)]
    emulator = {40: "qemu-arm", 183: "qemu-aarch64"}.get(machine)
    if not emulator or not shutil.which(emulator):
        raise ImageError(f"No emulator available for ELF machine {machine}: {path}")
    return [shutil.which(emulator), str(path)]


def _check_manifest(directory, manifest):
    expected = {}
    for line in manifest.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)", line)
        if not match or match[2] in expected:
            raise ImageError(f"Invalid module manifest: {manifest}")
        expected[match[2]] = match[1]
    actual = {p.name: _sha(p) for p in directory.iterdir() if p != manifest}
    if not expected or actual != expected:
        raise ImageError(f"Module manifest mismatch: {manifest}")


def _payload_modules(root, metadata):
    vendor = root / "module-files/vendor/lib/modules"
    early = root / "vendor_boot-files/ramdisk/lib/modules"
    _check_manifest(vendor, vendor / "modules.sha256")
    _check_manifest(early, root / "module-files/early.sha256")
    modules = sorted(vendor.glob("*.ko"))
    if not modules or not list(early.glob("*.ko")):
        raise ImageError("Payload has no vendor or early modules")
    layouts = set()
    for path in modules:
        module = _parse(path)
        if module.vermagic.split()[0] != metadata["kernel_release"]:
            raise ImageError(f"Module kernel release mismatch: {path.name}")
        versions = dict(module.versions)
        if "module_layout" not in versions or not module.required <= versions.keys():
            raise ImageError(f"Missing module ABI version records: {path.name}")
        layouts.add(versions["module_layout"])
    if len(layouts) != 1:
        raise ImageError("Modules disagree on the kernel module_layout ABI")
    for path in early.glob("*.ko"):
        if not (vendor / path.name).is_file() or _sha(path) != _sha(vendor / path.name):
            raise ImageError(f"Early module is not in the vendor set: {path.name}")
    image = (root / "boot-files/Image").read_bytes()
    if b"Linux version " + metadata["kernel_release"].encode() + b" " not in image:
        raise ImageError("Image kernel release does not match the module set")
    return vendor, early


def _avb(path, partition):
    text = _run(["avbtool", "info_image", "--image", path]).stdout.decode()
    def field(name, numeric=False):
        values = re.findall(r"^\s*" + re.escape(name) + r":\s*(.*?)\s*$", text, re.M)
        if len(values) != 1:
            raise ImageError(f"Ambiguous AVB {name}: {path}")
        if numeric:
            if not re.fullmatch(r"\d+(?: bytes)?", values[0]):
                raise ImageError(f"Invalid AVB {name}: {path}")
            return int(values[0].split()[0])
        return values[0]
    kind = "Hashtree" if partition == "vendor" else "Hash"
    descriptors = re.findall(r"^    (.* descriptor):", text, re.M)
    flags = re.findall(r"^\s*Flags:\s*(\d+)\s*$", text, re.M)
    if (field("Algorithm") != "NONE" or descriptors != [kind + " descriptor"]
            or field("Partition Name") != partition or field("Hash Algorithm") != "sha256"
            or len(flags) != 2 or int(flags[0]) != 0 or int(flags[1]) not in (0, 1)
            or field("Authentication Block", True) != 0):
        raise ImageError(f"Unsupported or signed AVB metadata: {path}")
    # Unknown descriptor types must not disappear during regeneration.
    tail = text.split("Descriptors:", 1)[1]
    for line in tail.splitlines():
        if line.startswith("    ") and not line.startswith("      ") and line.strip():
            if not (line.strip() == kind + " descriptor:" or line.strip().startswith("Prop: ")):
                raise ImageError(f"Unsupported AVB descriptor: {line.strip()}")
    props = re.findall(r"^    Prop: (.*?) -> '(.*)'$", text, re.M)
    if any("'" in value or "\x00" in key + value for key, value in props):
        raise ImageError("Unsupported AVB property encoding")
    result = {"size": field("Image size", True), "original": field("Original image size", True),
              "salt": field("Salt"), "rollback": field("Rollback Index", True),
              "location": field("Rollback Index Location", True), "flags": int(flags[1]),
              "props": props, "partition": partition}
    if (result["size"] != path.stat().st_size or result["original"] >= result["size"]
            or not re.fullmatch(r"[0-9a-f]+", result["salt"])):
        raise ImageError(f"Invalid AVB image dimensions or salt: {path}")
    if partition == "vendor":
        if (field("Version of dm-verity", True) != 1 or field("Data Block Size", True) != 4096
                or field("Hash Block Size", True) != 4096 or field("FEC num roots", True) != 2):
            raise ImageError("Unsupported vendor verity/FEC layout")
    _run(["avbtool", "verify_image", "--image", path])
    return result


def _footer(path, metadata, env):
    partition = metadata["partition"]
    command = ["avbtool", "add_hashtree_footer" if partition == "vendor" else "add_hash_footer",
               "--image", path, "--partition_size", str(metadata["size"]),
               "--partition_name", partition, "--algorithm", "NONE", "--hash_algorithm", "sha256",
               "--salt", metadata["salt"], "--rollback_index", str(metadata["rollback"]),
               "--rollback_index_location", str(metadata["location"]), "--flags", "0"]
    if metadata["flags"]:
        command.append("--do_not_use_ab")
    if partition == "vendor":
        command.extend(["--block_size", "4096", "--fec_num_roots", "2"])
    for key, value in metadata["props"]:
        command.extend(["--prop", key + ":" + value])
    _run(command, env=env)
    _run(["avbtool", "verify_image", "--image", path], env=env)
    if path.stat().st_size != metadata["size"]:
        raise ImageError(f"Rebuilt {partition} changed partition size")
    rebuilt = _avb(path, partition)
    for key in ("size", "salt", "rollback", "location", "flags", "props"):
        if rebuilt[key] != metadata[key]:
            raise ImageError(f"Rebuilt {partition} changed AVB {key}")


def _cpio(path):
    """Read newc entries for coverage and preservation checks, not extraction."""
    data = path.read_bytes()
    offset = 0
    entries = {}
    while offset + 110 <= len(data):
        header = data[offset:offset + 110]
        if header[:6] not in (b"070701", b"070702"):
            raise ImageError("Unsupported vendor ramdisk cpio")
        try:
            fields = [int(header[n:n + 8], 16) for n in range(6, 110, 8)]
        except ValueError as exc:
            raise ImageError("Malformed cpio header") from exc
        size, namesize = fields[6], fields[11]
        start = offset + 110
        rawname = data[start:start + namesize]
        if not namesize or len(rawname) != namesize or rawname[-1:] != b"\0":
            raise ImageError("Malformed cpio name")
        name = rawname[:-1].decode()
        start = (start + namesize + 3) & ~3
        content = data[start:start + size]
        offset = (start + size + 3) & ~3
        if len(content) != size:
            raise ImageError("Truncated cpio entry")
        if name == "TRAILER!!!":
            return entries
        name = name.removeprefix("./")
        if name in entries:
            raise ImageError(f"Duplicate cpio entry: {name}")
        entries[name] = (fields[1], fields[2], fields[3], content)
    raise ImageError("Missing cpio trailer")


def _load_names(data):
    try:
        lines = data.decode().splitlines()
    except UnicodeError as exc:
        raise ImageError("Non-text modules.load") from exc
    result = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_+-]+(?:\.ko)?", line):
            raise ImageError("Unsupported modules.load entry")
        result.append(line.removesuffix(".ko").replace("-", "_"))
    if len(result) != len(set(result)):
        raise ImageError("Duplicate modules.load entries")
    return result


def _coverage(names, replacements):
    original = {name for name in names if name.endswith(".ko")}
    wanted = {p.name for p in replacements.glob("*.ko")}
    if original != wanted:
        raise ImageError(f"Original and replacement module sets differ: {sorted(original ^ wanted)}")


def _header(path, vendor=False):
    with path.open("rb") as stream:
        data = stream.read(4096)
    if vendor:
        if data[:8] != b"VNDRBOOT" or len(data) < 2112 or struct.unpack_from("<I", data, 8)[0] != 3:
            raise ImageError("Only single-ramdisk vendor_boot header v3 is supported")
    elif data[:8] != b"ANDROID!" or len(data) < 1580 or struct.unpack_from("<I", data, 40)[0] != 3:
        raise ImageError("Only unsigned boot header v3 is supported")
    length = 2112 if vendor else 1580
    normalized = bytearray(data[:length])
    # Recompression may change ramdisk size even when its contents are retained.
    # All other header fields must remain byte-for-byte identical.
    if vendor:
        normalized[24:28] = bytes(4)
    else:
        normalized[8:16] = bytes(8)
    return bytes(normalized)


def _repack(source, target, workspace, magisk, *, kernel=None, early=None):
    original_header = _header(source, vendor=early is not None)
    workspace.mkdir()
    _run([*magisk, "unpack", source], cwd=workspace, codes=(3,) if early is not None else (0,))
    components = {p.name: _sha(p) for p in workspace.iterdir() if p.is_file()}
    before = None
    if kernel is not None:
        if "kernel" not in components:
            raise ImageError("Boot image has no kernel")
        shutil.copyfile(kernel, workspace / "kernel")
    else:
        cpio = workspace / "ramdisk.cpio"
        before = _cpio(cpio)
        prefix = "lib/modules/"
        module_entries = {name[len(prefix):]: value for name, value in before.items() if name.startswith(prefix)}
        if any("/" in name or not stat.S_ISREG(value[0]) for name, value in module_entries.items()):
            raise ImageError("Unsupported nested or nonregular early module entry")
        _coverage(module_entries, early)
        if "modules.load" not in module_entries or "modules.load" not in {p.name for p in early.iterdir()}:
            raise ImageError("Missing early module load order")
        order = _load_names(module_entries["modules.load"][3])
        if order != _load_names((early / "modules.load").read_bytes()):
            raise ImageError("Replacement changes early module load order")
        wanted_names = {p.stem.replace("-", "_") for p in early.glob("*.ko")}
        if set(order) != wanted_names:
            raise ImageError("Early modules.load does not cover the early module set")
        for path in sorted(early.iterdir()):
            if path.name == "modules.load":
                continue  # Preserve the original byte-for-byte, including comments.
            old = module_entries.get(path.name)
            if old and old[1:3] != (0, 0):
                raise ImageError("magiskboot cannot preserve non-root early module ownership")
            mode = stat.S_IMODE(old[0]) if old else 0o644
            _run([*magisk, "cpio", cpio, f"add {mode:04o} {prefix}{path.name} {path}"], cwd=workspace)
        after = _cpio(cpio)
        for name, entry in before.items():
            if not name.startswith(prefix) or name == prefix + "modules.load":
                if after.get(name) != entry:
                    raise ImageError(f"Unrelated ramdisk entry changed: {name}")
        for path in early.iterdir():
            if path.name != "modules.load" and after.get(prefix + path.name, (None,) * 4)[3] != path.read_bytes():
                raise ImageError(f"Early module write failed: {path.name}")
    env = os.environ.copy()
    env.pop("PATCHVBMETAFLAG", None)
    _run([*magisk, "repack", source, target], cwd=workspace, env=env)
    # Re-unpack the finished image to check actual serialized components.
    check = workspace.parent / (workspace.name + "-check")
    check.mkdir()
    _run([*magisk, "unpack", target], cwd=check, codes=(3,) if early is not None else (0,))
    expected = {p.name: _sha(p) for p in workspace.iterdir() if p.is_file()}
    actual = {p.name: _sha(p) for p in check.iterdir() if p.is_file()}
    if actual != expected:
        raise ImageError("Repacked image did not preserve its unpacked components")
    if _header(target, vendor=early is not None) != original_header:
        raise ImageError("Repacked image changed unrelated boot header fields")


def _debug(image, command, write=False):
    result = _run(["debugfs", *(["-w"] if write else []), "-R", command, image])
    # debugfs often returns zero even when its requested operation failed.
    errors = result.stderr.decode(errors="replace")
    if any(re.search(pattern, errors, re.I) for pattern in
           (r"not found", r"no such", r"not enough", r"could not", r"error", r"short (?:read|write)",
            r"file exists", r"usage:", r"while ", r"invalid", r"unsupported")):
        raise ImageError(f"debugfs operation failed: {command}: {errors}")
    return result.stdout


def _attrs(image, name, scratch):
    text = _debug(image, f'stat "{name}"').decode()
    mode = re.search(r"Type:\s+(regular|directory)\s+Mode:\s+([0-7]+)", text)
    ids = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+)", text)
    if not mode or not ids or "Links: 1 " not in text and mode[1] == "regular":
        raise ImageError(f"Unsupported inode metadata: {name}")
    attr = scratch / "selinux"
    attr.unlink(missing_ok=True)
    _debug(image, f'ea_get -f "{attr}" "{name}" security.selinux')
    if not attr.is_file() or not attr.read_bytes():
        raise ImageError(f"Missing SELinux context: {name}")
    return (int(mode[2], 8), int(ids[1]), int(ids[2]), attr.read_bytes())


def _dump(image, name, destination):
    destination.unlink(missing_ok=True)
    _debug(image, f'dump "{name}" "{destination}"')
    if not destination.is_file():
        raise ImageError(f"debugfs did not read back {name}")


def _vendor(image, replacements, workspace):
    workspace.mkdir()
    _run(["e2fsck", "-fn", image])
    stats = _debug(image, "stats").decode()
    if "shared_blocks" in stats or "needs_recovery" in stats:
        raise ImageError("Unsupported shared-block or journal-recovery vendor filesystem")
    listing = _debug(image, 'ls -p /lib/modules').decode()
    names = []
    for line in listing.splitlines():
        if not line.strip():
            continue
        fields = line.split("/")
        if len(fields) < 7:
            raise ImageError("Cannot parse vendor module directory")
        name = fields[5]
        if name not in (".", ".."):
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name) or not stat.S_ISREG(int(fields[2], 8)):
                raise ImageError("Unsupported vendor module directory entry")
            names.append(name)
    _coverage(names, replacements)
    directory = _attrs(image, "/lib/modules", workspace)
    metadata = {}
    for path in replacements.iterdir():
        metadata[path.name] = (_attrs(image, "/lib/modules/" + path.name, workspace)
                               if path.name in names else (0o644, 0, directory[2], directory[3]))
    load = workspace / "original.modules.load"
    if "modules.load" not in names:
        raise ImageError("Original vendor has no modules.load")
    _dump(image, "/lib/modules/modules.load", load)
    order = _load_names(load.read_bytes())
    if not set(order) <= {p.stem.replace("-", "_") for p in replacements.glob("*.ko")}:
        raise ImageError("Original vendor load order names modules outside replacement set")
    if (replacements / "modules.load").exists() and (replacements / "modules.load").read_bytes() != load.read_bytes():
        raise ImageError("Replacement changes vendor modules.load")
    # Reclaim all replacement blocks before allocating any new module.
    for path in replacements.iterdir():
        if path.name in names and path.name != "modules.load":
            _debug(image, f'rm "/lib/modules/{path.name}"', True)
    for path in sorted(replacements.iterdir()):
        if path.name == "modules.load":
            continue
        name = "/lib/modules/" + path.name
        mode, uid, gid, context = metadata[path.name]
        _debug(image, f'write "{path}" "{name}"', True)
        for field, value in (("mode", 0o100000 | mode), ("uid", uid), ("gid", gid)):
            _debug(image, f'sif "{name}" {field} {value}', True)
        context_file = workspace / "context"
        context_file.write_bytes(context)
        _debug(image, f'ea_set -f "{context_file}" "{name}" security.selinux', True)
    for path in replacements.iterdir():
        name = "/lib/modules/" + path.name
        readback = workspace / "readback"
        _dump(image, name, readback)
        if _sha(readback) != _sha(path) or _attrs(image, name, workspace) != metadata[path.name]:
            raise ImageError(f"Vendor file or inode verification failed: {path.name}")
    check_load = workspace / "check.modules.load"
    _dump(image, "/lib/modules/modules.load", check_load)
    if check_load.read_bytes() != load.read_bytes():
        raise ImageError("Vendor modules.load changed")
    _run(["e2fsck", "-fn", image])


def build_images(payload: Path, boot: Path, vendor_boot: Path, vendor: Path, output: Path) -> Path:
    """Build and atomically publish boot.img, vendor_boot.img and vendor.img."""
    payload, boot, vendor_boot, vendor, output = map(Path, (payload, boot, vendor_boot, vendor, output))
    if output.exists() or output.is_symlink():
        raise ImageError(f"Refusing to overwrite output: {output}")
    inputs = {"payload": payload, "boot": boot, "vendor_boot": vendor_boot, "vendor": vendor}
    try:
        inputs = {name: path.resolve(strict=True) for name, path in inputs.items()}
        if any(not path.is_file() for path in inputs.values()):
            raise ImageError("Every input must be a regular file")
        output = output.absolute()
        if not output.parent.is_dir():
            raise ImageError("Output parent directory must already exist")
        for dependency in ("avbtool", "debugfs", "e2fsck"):
            if not shutil.which(dependency):
                raise ImageError(f"Required host dependency is unavailable: {dependency}")
        with tempfile.TemporaryDirectory(prefix=".capri-images-", dir=output.parent) as temporary:
            work = Path(temporary)
            root = work / "payload"
            root.mkdir()
            metadata = _extract(inputs["payload"], root)
            replacements, early = _payload_modules(root, metadata)
            magisk = _tool(root / "tools/magiskboot")
            host_fec = shutil.which("fec")
            fec = [host_fec] if host_fec else _tool(root / "tools/fec")
            bin_dir = work / "bin"
            bin_dir.mkdir()
            wrapper = bin_dir / "fec"
            wrapper.write_text("#!/bin/sh\nexec " + shlex.join(fec) + ' "$@"\n')
            wrapper.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            original_hashes = {name: _sha(path) for name, path in inputs.items()}
            avb = {name: _avb(inputs[name], name) for name in ("boot", "vendor_boot", "vendor")}
            staged = work / "images"
            staged.mkdir()
            # Work only on private copies, stripped to each original AVB data size.
            copies = {}
            for name in avb:
                path = work / (name + ".base.img")
                shutil.copyfile(inputs[name], path)
                with path.open("r+b") as stream:
                    stream.truncate(avb[name]["original"])
                copies[name] = path
            _repack(copies["boot"], staged / "boot.img", work / "boot-work", magisk,
                    kernel=root / "boot-files/Image")
            _repack(copies["vendor_boot"], staged / "vendor_boot.img", work / "vendor-boot-work", magisk,
                    early=early)
            shutil.move(copies["vendor"], staged / "vendor.img")
            _vendor(staged / "vendor.img", replacements, work / "vendor-work")
            for name in avb:
                _footer(staged / (name + ".img"), avb[name], env)
            if original_hashes != {name: _sha(path) for name, path in inputs.items()}:
                raise ImageError("An input changed during image construction")
            manifest = {
                "format": "kernelforge-capri-images-v1", "device": "capri",
                "kernel_release": metadata["kernel_release"],
                "inputs": {name: {"sha256": original_hashes[name], "size": path.stat().st_size}
                           for name, path in inputs.items()},
                "images": {p.name: {"sha256": _sha(p), "size": p.stat().st_size}
                           for p in sorted(staged.iterdir())},
                "limitations": ["External ROM vbmeta trust and device boot compatibility are unverified.",
                                "Regenerated per-image AVB metadata is unsigned (Algorithm NONE).",
                                "No device vbmeta, verification flags, or device partitions were modified."],
            }
            (staged / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            if output.exists() or output.is_symlink():
                raise ImageError("Output appeared during image construction")
            # renameat2 NOREPLACE avoids replacing even an empty directory in a race.
            _publish(staged, output)
            return output
    except (OSError, ModuleError, ValueError, struct.error) as exc:
        raise ImageError(f"Cannot build Capri images: {exc}") from exc


def _publish(source, destination):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ImageError("Atomic no-replace publication requires Linux renameat2")
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise ImageError(f"Cannot publish images without replacing output: {os.strerror(error)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("payload", "boot", "vendor-boot", "vendor", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_images(args.payload, args.boot, args.vendor_boot, args.vendor, args.output)
    except ImageError as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(result)


if __name__ == "__main__":
    main()
