"""YAML-driven Android kernel builder. All modified Git trees live under .work/."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile

import yaml

from .modules import ModuleError, stage_modules
from .root import RootError, prepare_bakasu

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work"
CONFIG = ROOT / "kernel.yaml"
DEVICES = ROOT / "devices"
DEFAULT_DEVICE = "lemonades"
WORK_NAME = "kernel"
SHA = re.compile(r"^[a-f0-9]{40}$")
SUPPORTED = {"apiVersion", "source", "toolchain", "root", "build", "droidspaces", "overlays", "patches", "package"}
LLVM_TOOLS = ("clang", "clang++", "ld.lld", "llvm-ar", "llvm-nm",
              "llvm-objcopy", "llvm-objdump", "llvm-readelf", "llvm-size", "llvm-strip")
BAKASU_PATCH = "patches/0002-bakasu-manual-hooks-4.19.patch"
BAKASU_TARGET = "vendor/bakasu.config"


class ForgeError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ForgeError(message)


def fields(value, allowed, label):
    require(isinstance(value, dict), label + " must be a mapping")
    unknown = set(value) - set(allowed)
    require(not unknown, label + ": unknown keys " + ", ".join(sorted(unknown)))


def relative(value, label):
    require(isinstance(value, str) and value, label + " must be a nonempty relative path")
    p = Path(value)
    require(not p.is_absolute() and not any(x in ("..", ".") for x in p.parts),
            label + " must not be absolute or contain . or ..")
    require(p.parts[0] not in (".git", ".work"), label + " points at protected directory")
    return p


def local_path(value, label):
    p = relative(value, label)
    resolved = (ROOT / p).resolve()
    require(resolved.is_relative_to(ROOT), label + " escapes repository")
    return resolved


def validate_item(item, allowed, label):
    fields(item, allowed, label)
    require(type(item.get("enabled", True)) is bool, label + ".enabled must be boolean")


def validate_package(cfg):
    """Validate AnyKernel3 configuration before any Git or filesystem changes."""
    fields(cfg, {"type", "output", "device_check", "device", "anykernel", "boot", "images", "modules"}, "package")
    require(cfg.get("type") == "anykernel3", "package.type must be anykernel3")
    output = cfg.get("output")
    require(isinstance(output, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.zip", output)),
            "package.output must be a safe ZIP filename")
    require(type(cfg.get("device_check", False)) is bool,
            "package.device_check must be true or false")

    device = cfg.get("device")
    fields(device, {"name", "codenames", "android_versions"}, "package.device")
    name = device.get("name")
    require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,90}", name)),
            "package.device.name must be an ASCII label without shell metacharacters")
    codes = device.get("codenames")
    require(isinstance(codes, list) and 1 <= len(codes) <= 20 and
            all(isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", c) for c in codes)
            and len(set(codes)) == len(codes),
            "package.device.codenames must contain unique Android device codenames")
    versions = device.get("android_versions")
    require(isinstance(versions, list) and bool(versions) and
            all(isinstance(v, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", v) for v in versions),
            "package.device.android_versions must be version strings, e.g. ['17']")

    upstream = cfg.get("anykernel")
    fields(upstream, {"url", "ref", "commit"}, "package.anykernel")
    require(isinstance(upstream.get("url"), str) and upstream["url"].startswith("https://"),
            "package.anykernel.url must use HTTPS Git")
    require(isinstance(upstream.get("ref"), str) and bool(upstream["ref"]),
            "package.anykernel.ref is required")
    require(isinstance(upstream.get("commit"), str) and bool(SHA.fullmatch(upstream["commit"])),
            "package.anykernel.commit must be a pinned 40-character SHA")

    boot = cfg.get("boot")
    fields(boot, {"partition", "slots", "ramdisk", "vbmeta", "install"}, "package.boot")
    require(isinstance(boot.get("partition"), str) and
            bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", boot["partition"])),
            "package.boot.partition must be a by-name partition (e.g. boot)")
    require(boot.get("slots") in ("auto", "ab", "single"),
            "package.boot.slots must be auto, ab or single")
    require(boot.get("ramdisk") in ("auto", "gz", "lzo", "lzma", "xz", "bz2", "lz4", "lz4-l", "cpio", "none"),
            "package.boot.ramdisk is an unsupported compression")
    require(str(boot.get("vbmeta")) in ("auto", "0", "1"),
            "package.boot.vbmeta must be auto, '0' or '1'")
    require(boot.get("install") in ("write_boot", "flash_boot"),
            "package.boot.install must be write_boot or flash_boot")
    if "modules" in cfg:
        modules = cfg["modules"]
        fields(modules, {"aliases", "vendor_boot"}, "package.modules")
        require(cfg.get("device_check") is True,
                "Matched-module installation requires package.device_check: true")
        require(boot["partition"] == "boot" and boot["install"] == "write_boot" and
                boot["slots"] in ("auto", "ab"),
                "Matched modules require slotted boot with write_boot installation")
        aliases = modules.get("aliases", {})
        require(isinstance(aliases, dict), "package.modules.aliases must be a mapping")
        early = modules.get("vendor_boot")
        require(isinstance(early, list) and early and
                all(isinstance(name, str) for name in early) and
                len(early) == len(set(early)),
                "package.modules.vendor_boot must list unique early module filenames")
        for filename in [*aliases, *aliases.values(), *early]:
            require(isinstance(filename, str) and
                    bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.ko", filename)),
                    "Module names must be plain .ko filenames")

    images = cfg.get("images")
    require(isinstance(images, dict) and bool(images),
            "package.images must map kernel output paths to ZIP paths")
    require(any(isinstance(dst, str) and
                re.fullmatch(r"(Image(?:[._-][A-Za-z0-9._-]+)?|zImage(?:[._-][A-Za-z0-9._-]+)?)", dst)
                for dst in images.values()),
            "package.images must contain a kernel Image or zImage destination")
    destinations = []
    for src, dst in images.items():
        relative(src, "package image source")
        path = relative(dst, "package image destination")
        require(path.parts[0] not in ("tools", "META-INF", "modules", "ramdisk", "patch",
                                     "module-files", "boot-files", "vendor_boot-files") and
                path.as_posix() != "anykernel.sh",
                "package.images cannot overwrite AnyKernel3 installer files")
        destinations.append(path.as_posix())
    require(len(destinations) == len(set(destinations)),
            "package.images ZIP destinations must be unique")
    if "modules" in cfg:
        require(len(destinations) == 1 and destinations[0] == "Image",
                "Matched-module packages replace only Image and preserve installed DTB/DTBO")


def merge_settings(common, device):
    """Recursively merge device mappings; lists replace defaults."""
    merged = dict(common)
    for key, value in device.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def workspace_name(device):
    return WORK_NAME if device == DEFAULT_DEVICE else "kernel-" + device


def load_config(device=DEFAULT_DEVICE):
    require(isinstance(device, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", device)),
            "Invalid device codename")
    require(CONFIG.is_file(), "Missing kernel.yaml")
    common = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fields(common, SUPPORTED, "kernel.yaml")
    require(common.get("apiVersion") == "kernelforge/v1", "Unsupported apiVersion")
    require(common.get("droidspaces") is not None, "Shared DroidSpaces configuration is required")
    require(common.get("patches") is not None, "Shared patch list is required")
    device_path = DEVICES / (device + ".yaml")
    require(device_path.is_file(), "Unknown device: " + device + " (missing " + str(device_path) + ")")
    overrides = yaml.safe_load(device_path.read_text(encoding="utf-8"))
    fields(overrides, {"source", "build", "package", "overlays"}, "devices/" + device + ".yaml")
    require(isinstance(overrides.get("source"), dict), "Device source is required")
    require(isinstance(overrides.get("build"), dict) and "defconfig" in overrides["build"],
            "Device defconfig is required")
    require(isinstance(overrides.get("package"), dict) and "device" in overrides["package"],
            "Device AnyKernel3 metadata is required")
    data = merge_settings(common, overrides)
    fields(data, SUPPORTED, "merged kernel configuration")
    require(data.get("apiVersion") == "kernelforge/v1", "Unsupported apiVersion")
    require(device in data["package"]["device"].get("codenames", []),
            "Selected device does not match its AnyKernel3 codename")
    # DroidSpaces is mandatory and has no enabled/disabled switch.
    droidspaces = data.get("droidspaces")
    fields(droidspaces, {"config", "destination", "target"}, "droidspaces")
    local_path(droidspaces.get("config"), "droidspaces.config")
    relative(droidspaces.get("destination"), "droidspaces.destination")
    relative(droidspaces.get("target"), "droidspaces.target")

    src = data.get("source")
    fields(src, {"url", "ref"}, "source")
    require(isinstance(src.get("url"), str) and src["url"].startswith("https://"),
            "source.url must be an HTTPS Git URL")
    require(isinstance(src.get("ref"), str) and bool(src["ref"]), "source.ref is required")
    root = data.get("root")
    fields(root, {"enabled", "url", "ref", "commit"}, "root")
    require(type(root.get("enabled")) is bool, "root.enabled must be boolean")
    require(isinstance(root.get("url"), str) and root["url"].startswith("https://"),
            "root.url must be an HTTPS Git URL")
    require(isinstance(root.get("ref"), str) and bool(root["ref"]), "root.ref is required")
    require(isinstance(root.get("commit"), str) and bool(SHA.fullmatch(root["commit"])),
            "root.commit must be a pinned 40-character BakaSU SHA")


    tc = data.get("toolchain")
    fields(tc, {"url", "ref", "commit", "directory", "sha256"}, "toolchain")
    require(isinstance(tc.get("url"), str) and tc["url"].startswith("https://"),
            "toolchain.url must be HTTPS")
    require(isinstance(tc.get("ref"), str) and bool(tc["ref"]),
            "toolchain.ref is required")
    if tc.get("commit") is not None:
        require(isinstance(tc["commit"], str) and bool(SHA.fullmatch(tc["commit"])),
                "toolchain.commit must be a full 40-character SHA")
    if "directory" in tc:
        relative(tc["directory"], "toolchain.directory")
        require(tc.get("commit") is not None, "toolchain.commit is required for a directory archive")
    if "sha256" in tc:
        require(isinstance(tc["sha256"], str) and bool(re.fullmatch(r"[a-f0-9]{64}", tc["sha256"])),
                "toolchain.sha256 must be a 64-character SHA256 digest")
        require(not tc.get("directory"), "toolchain.sha256 cannot be combined with toolchain.directory")

    build = data.get("build")
    fields(build, {"arch", "defconfig", "fragments", "targets", "artifacts", "ccache"}, "build")
    require(build.get("arch") in ("arm64", "arm"), "build.arch must be arm or arm64")
    relative(build.get("defconfig"), "build.defconfig")
    require(isinstance(build.get("fragments", []), list), "build.fragments must be a list")
    for item in build.get("fragments", []):
        relative(item, "build.fragments entry")
    require(isinstance(build.get("targets", []), list), "build.targets must be a list")
    for item in build.get("targets", []):
        require(isinstance(item, str) and bool(re.fullmatch(r"[A-Za-z0-9_./-]+", item)),
                "Invalid build target")
    require(isinstance(build.get("artifacts", []), list), "build.artifacts must be a list")
    for item in build.get("artifacts", []):
        relative(item, "build.artifacts entry")
    cache = build.get("ccache", {"enabled": False})
    validate_item(cache, {"enabled", "max_size"}, "build.ccache")
    size = cache.get("max_size", "2G")
    require(isinstance(size, str) and bool(re.fullmatch(r"[1-9][0-9]*[KMGT]", size)),
            "build.ccache.max_size must be a size such as 2G or 512M")

    for n, item in enumerate(data.get("overlays", [])):
        validate_item(item, {"from", "to", "enabled"}, "overlay " + str(n))
        local_path(item.get("from"), "overlay.from")
        relative(item.get("to"), "overlay.to")
    for n, item in enumerate(data.get("patches", [])):
        validate_item(item, {"file", "enabled"}, "patch " + str(n))
        local_path(item.get("file"), "patch.file")

    validate_package(data.get("package"))
    return data


def run(argv, cwd=None, env=None, dry=False, capture=False):
    msg = "+ " + shlex.join([str(a) for a in argv])
    if cwd:
        msg += "  (cwd: " + str(cwd) + ")"
    print(msg, flush=True)
    if dry:
        return ""
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, text=True,
                            check=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def managed(name, dry=False):
    folder = WORK / name
    marker = folder / ".kernelforge-managed"
    if folder.exists():
        require(marker.is_file(), "Refusing to modify unmarked working directory: " + str(folder))
    elif not dry:
        folder.mkdir(parents=True)
        marker.write_text("Managed by KernelForge; prepare resets only src/.\n", encoding="utf-8")
    return folder


def fetch_source(name, data, dry=False):
    folder = managed(name, dry)
    source = folder / "src"
    if not source.exists():
        if not dry:
            source.mkdir(parents=True)
        run(["git", "init", str(source)], dry=dry)
        run(["git", "-C", str(source), "remote", "add", "origin", data["source"]["url"]], dry=dry)
    else:
        require((source / ".git").exists(), "Managed source directory is not a Git checkout")
        # Only src/ is disposable. Preserve out/, artifacts and toolchain caches.
        run(["git", "-C", str(source), "remote", "set-url", "origin", data["source"]["url"]], dry=dry)
        run(["git", "-C", str(source), "reset", "--hard"], dry=dry)
        run(["git", "-C", str(source), "clean", "-fdx"], dry=dry)
    ref = data["source"]["ref"]
    run(["git", "-C", str(source), "fetch", "--depth=1", "origin", ref], dry=dry)
    # Always use the branch head fetched in this run, never a pinned commit.
    commit = run(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"],
                 dry=dry, capture=True)
    run(["git", "-C", str(source), "checkout", "--detach", commit or "FETCH_HEAD"], dry=dry)
    return source, commit


def prepare(name, data, dry=False):
    source, commit = fetch_source(name, data, dry)
    root_commit = None
    patches = list(data.get("patches", []))
    if data["root"]["enabled"]:
        root_commit = prepare_bakasu(source, data["root"], run, dry=dry)
        patches.append({"file": BAKASU_PATCH})
    for entry in patches:
        if not entry.get("enabled", True):
            continue
        patch = local_path(entry["file"], "patch.file")
        require(patch.is_file() or dry, "Missing patch: " + str(patch))
        run(["git", "-C", str(source), "apply", "--check", str(patch)], dry=dry)
        run(["git", "-C", str(source), "apply", str(patch)], dry=dry)
    mandatory = {"from": data["droidspaces"]["config"],
                 "to": data["droidspaces"]["destination"], "enabled": True}
    overlays = [mandatory]
    if data["root"]["enabled"]:
        overlays.append({"from": "assets/bakasu.config",
                         "to": "arch/arm64/configs/" + BAKASU_TARGET})
    for entry in overlays + data.get("overlays", []):
        if not entry.get("enabled", True):
            continue
        origin = local_path(entry["from"], "overlay.from")
        dest = (source / relative(entry["to"], "overlay.to")).resolve()
        require(dest.is_relative_to(source.resolve()) and ".git" not in dest.parts,
                "Unsafe overlay destination")
        require(origin.is_file() or dry, "Missing overlay: " + str(origin))
        print("COPY", origin, "->", dest)
        if not dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, dest)
    if not dry:
        state = {"source": data["source"]["url"], "ref": data["source"]["ref"],
                 "commit": commit, "patches": [p["file"] for p in patches if p.get("enabled", True)],
                 "root": {"enabled": True, "provider": "BakaSU", "commit": root_commit}
                         if root_commit else {"enabled": False}}
        (WORK / name / "prepared.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return source


def bootstrap_toolchain(name, data, dry=False):
    """Return one LLVM bin directory for every build phase."""
    override = os.environ.get("KERNELFORGE_LLVM_BIN")
    if override:
        bindir = Path(override).expanduser().resolve()
        require(bindir.is_dir() or dry, "LLVM override directory not found: " + str(bindir))
    else:
        spec = data["toolchain"]
        dest = WORK / "_toolchains" / WORK_NAME / "llvm"  # Shared by every device.
        if spec.get("sha256") or spec.get("directory"):
            # Prefer a small, checksum-verified GitHub release archive over Gitiles.
            archive_url = (spec["url"] if spec.get("sha256") else
                           spec["url"].rstrip("/") + "/+archive/" + spec["commit"]
                           + "/" + spec["directory"] + ".tar.gz")
            stamp = dest / ".kernelforge-toolchain"
            identity = "\n".join((archive_url, spec.get("sha256", ""))) + "\n"
            if not stamp.is_file() or stamp.read_text(encoding="utf-8") != identity:
                print("DOWNLOAD", archive_url, "->", dest, flush=True)
                if not dry:
                    require(not dest.is_symlink(), "LLVM toolchain path must not be a symlink")
                    if dest.exists():
                        shutil.rmtree(dest)
                    dest.mkdir(parents=True, exist_ok=True)
                    try:
                        if spec.get("sha256"):
                            with tempfile.TemporaryFile() as archive_file:
                                digest = hashlib.sha256()
                                with urlopen(archive_url, timeout=120) as response:
                                    while chunk := response.read(1024 * 1024):
                                        archive_file.write(chunk)
                                        digest.update(chunk)
                                require(digest.hexdigest() == spec["sha256"],
                                        "LLVM release SHA256 mismatch: " + archive_url)
                                archive_file.seek(0)
                                with tarfile.open(fileobj=archive_file, mode="r:gz") as bundle:
                                    bundle.extractall(dest, filter="data")
                        else:
                            with urlopen(archive_url, timeout=120) as response:
                                with tarfile.open(fileobj=response, mode="r|gz") as bundle:
                                    bundle.extractall(dest, filter="data")
                        stamp.write_text(identity, encoding="utf-8")
                    except Exception:
                        shutil.rmtree(dest)
                        raise
            bindir = dest / "bin"
        else:
            if not dest.exists():
                if not dry:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                run(["git", "clone", "--depth=1", "--branch", spec["ref"],
                     spec["url"], str(dest)], dry=dry)
            else:
                require((dest / ".git").exists(), "LLVM path is not a Git clone: " + str(dest))
                run(["git", "-C", str(dest), "remote", "set-url", "origin", spec["url"]], dry=dry)
                run(["git", "-C", str(dest), "fetch", "--depth=1", "origin", spec["ref"]], dry=dry)
            if spec.get("commit"):
                run(["git", "-C", str(dest), "fetch", "--depth=1",
                     "origin", spec["commit"]], dry=dry)
                run(["git", "-C", str(dest), "checkout", "--detach",
                     spec["commit"]], dry=dry)
            else:
                run(["git", "-C", str(dest), "checkout", "--detach", "FETCH_HEAD"], dry=dry)
            bindir = dest / "bin"

    missing = [tool for tool in LLVM_TOOLS if not (bindir / tool).is_file()]
    require(not missing or dry, "LLVM toolchain incomplete at " + str(bindir)
            + ": missing " + ", ".join(missing))
    return bindir


def build_environment(data, llvm_bin):
    env = os.environ.copy()
    env["PATH"] = str(llvm_bin) + os.pathsep + env.get("PATH", "")
    env["ARCH"] = data["build"]["arch"]
    env["LLVM"] = "1"
    env["LLVM_IAS"] = "1"
    # No GCC binaries, prefixes or mixed compiler toolchains.
    for var in ("CROSS_COMPILE", "CROSS_COMPILE_ARM32", "CLANG_TRIPLE"):
        env.pop(var, None)
    cache = data["build"].get("ccache", {})
    if cache.get("enabled", False):
        env.setdefault("CCACHE_DIR", str(WORK / "_ccache"))
        env.setdefault("CCACHE_BASEDIR", str(ROOT))
        env.setdefault("CCACHE_COMPILERCHECK", "content")
        env.setdefault("CCACHE_COMPRESS", "true")
        env.setdefault("CCACHE_MAXSIZE", cache.get("max_size", "2G"))
    return env


def build(name, data, jobs, dry=False):
    src = prepare(name, data, dry=dry)
    llvm_bin = bootstrap_toolchain(name, data, dry=dry)
    env = build_environment(data, llvm_bin)
    out = WORK / name / "out"
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
    base = ["make", "ARCH=" + data["build"]["arch"], "O=" + str(out.resolve()),
            "LLVM=1", "LLVM_IAS=1"]
    if data["build"].get("ccache", {}).get("enabled", False):
        require(shutil.which("ccache", path=env["PATH"]) is not None or dry,
                "ccache enabled in kernel.yaml but executable not found. Install ccache or disable build.ccache.")
        # Command-line variables override the kernel's CC/HOSTCC defaults.
        # Keep the same LLVM compiler for kernel and host compilation.
        base.extend(["CC=ccache clang", "HOSTCC=ccache clang",
                     "HOSTCXX=ccache clang++"])
    config_targets = ([data["build"]["defconfig"]] + data["build"].get("fragments", [])
                      + [data["droidspaces"]["target"]])
    if data["root"]["enabled"]:
        config_targets.append(BAKASU_TARGET)
    run(base + config_targets, cwd=src, env=env, dry=dry)
    run(base + ["-j" + str(jobs)] + data["build"].get("targets", []),
        cwd=src, env=env, dry=dry)
    if "modules" in data["package"] and data["build"].get("targets") and "modules" not in data["build"]["targets"]:
        run(base + ["-j" + str(jobs), "modules"], cwd=src, env=env, dry=dry)
    if data["build"].get("ccache", {}).get("enabled", False):
        run(["ccache", "--show-stats"], env=env, dry=dry)
    if not dry:
        for item in data["build"].get("artifacts", []):
            artifact = (out / relative(item, "build.artifacts entry")).resolve()
            require(artifact.is_relative_to(out.resolve()) and artifact.is_file(),
                    "Required artifact missing after build: " + str(artifact))
    print("Build output:", out)
    return out


def bootstrap_anykernel(data, dry=False):
    """Fetch the pinned upstream AnyKernel3 template, never modifying its checkout."""
    spec = data["package"]["anykernel"]
    folder = WORK / "_anykernel3"
    if not folder.exists():
        if not dry:
            folder.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth=1", "--branch", spec["ref"],
             spec["url"], str(folder)], dry=dry)
    else:
        require((folder / ".git").is_dir(), "AnyKernel3 template path is not a Git checkout")
        run(["git", "-C", str(folder), "remote", "set-url", "origin", spec["url"]], dry=dry)
    run(["git", "-C", str(folder), "fetch", "--depth=1", "origin", spec["commit"]], dry=dry)
    run(["git", "-C", str(folder), "checkout", "--detach", spec["commit"]], dry=dry)
    return folder


def render_anykernel(cfg):
    """Generate an AnyKernel3 installer for the exact device declared in YAML."""
    device = cfg["device"]
    boot = cfg["boot"]
    names = "\n".join(f"device.name{i}={name}" for i, name in enumerate(device["codenames"], 1))
    versions = ", ".join(device["android_versions"])
    slot = {"auto": "auto", "ab": "1", "single": "0"}[boot["slots"]]
    install = ("dump_boot;\nwrite_boot;" if boot["install"] == "write_boot" else
               "split_boot;\nflash_boot;\nflash_dtbo;")
    if "modules" in cfg:
        install = (". tools/install-modules.sh;\nkernelforge_begin_install;\n"
                   "dump_boot;\nwrite_boot;\nkernelforge_check_boot;\n"
                   "BLOCK=vendor_boot;\nreset_ak;\ndump_boot;\n"
                   "kernelforge_prepare_vendor_boot;\nwrite_boot;\n"
                   "kernelforge_finish_install;")
    return (f"### Generated by KernelForge from kernel.yaml; do not edit in ZIP.\n"
            f"## AnyKernel3 installer, based on osm0sis/AnyKernel3.\n"
            f"properties() {{ '\n"
            f"kernel.string={device['name']} - DroidSpaces Kernel\n"
            f"do.devicecheck={int(cfg.get('device_check', False))}\ndo.modules=0\ndo.systemless=0\n"
            f"do.cleanup=1\ndo.cleanuponabort=0\n"
            f"{names}\n"
            f"supported.versions={versions}\n"
            f"supported.patchlevels=\nsupported.vendorpatchlevels=\n"
            f"'; }}\n\n"
            f"BLOCK={boot['partition']};\n"
            f"IS_SLOT_DEVICE={slot};\n"
            f"RAMDISK_COMPRESSION={boot['ramdisk']};\n"
            f"PATCH_VBMETA_FLAG={boot['vbmeta']};\n"
            f". tools/ak3-core.sh;\n\n{install}\n")


def package(name, data, dry=False):
    """Always produce a YAML-configured AnyKernel3 ZIP (device check defaults off)."""
    cfg = data["package"]
    folder = managed(name, dry)
    artifact = folder / cfg["output"]
    template = bootstrap_anykernel(data, dry=dry)
    print("PACKAGE AnyKernel3", cfg["device"]["codenames"], "->", artifact)
    if dry:
        for src, dst in cfg["images"].items():
            print("IMAGE", src, "->", dst)
        if "modules" in cfg:
            print("MODULES modules.order -> vendor/lib/modules and vendor_boot ramdisk")
        return artifact

    require((template / "tools" / "ak3-core.sh").is_file(),
            "AnyKernel3 template missing tools/ak3-core.sh")
    require((template / "META-INF" / "com" / "google" / "android" / "update-binary").is_file(),
            "AnyKernel3 template missing recovery updater")
    staging = folder / "package-staging"
    if staging.exists():
        shutil.rmtree(staging)
    for entry in template.rglob("*"):
        require(not entry.is_symlink(), "Refusing AnyKernel3 template symlink: " + str(entry))
    shutil.copytree(template, staging, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "README.md", "*placeholder*"))
    (staging / "anykernel.sh").write_text(render_anykernel(cfg), encoding="utf-8")

    out = folder / "out"
    for src_image, dst_image in cfg["images"].items():
        source = (out / relative(src_image, "package image source")).resolve()
        require(source.is_relative_to(out.resolve()) and source.is_file(),
                "Missing required kernel output: " + str(source))
        dest = (staging / relative(dst_image, "package image destination")).resolve()
        require(dest.is_relative_to(staging.resolve()), "Unsafe AnyKernel3 destination")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    if "modules" in cfg:
        llvm_bin = bootstrap_toolchain(name, data)
        stage_modules(out, staging, cfg["modules"], strip_tool=llvm_bin / "llvm-strip")
        boot_files = staging / "boot-files"
        boot_files.mkdir()
        shutil.move(str(staging / "Image"), boot_files / "Image")
        shutil.copyfile(ROOT / "assets/install-modules.sh", staging / "tools/install-modules.sh")

    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for p in sorted(staging.rglob("*")):
            rel = p.relative_to(staging)
            if ".git" in rel.parts or p.name == "README.md" or "placeholder" in p.name.lower():
                continue
            require(not p.is_symlink(), "Unexpected symlink in AnyKernel3 template: " + str(rel))
            if p.is_file():
                zf.write(p, rel.as_posix())
    shutil.rmtree(staging)
    print("Created:", artifact)
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description="KernelForge: shared DroidSpaces, device-specific kernels")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "prepare", "toolchain", "build", "package"):
        sub = subs.add_parser(command)
        sub.add_argument("--device", default=DEFAULT_DEVICE, help="Device YAML codename (default: lemonades)")
        if command in ("prepare", "toolchain", "build", "package"):
            sub.add_argument("--dry-run", action="store_true", help="Print actions without writing or downloading")
        if command == "build":
            sub.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
            sub.add_argument("--no-package", action="store_true",
                             help="Compile only; package separately after persisting build caches")
    opts = parser.parse_args(argv)
    try:
        data = load_config(opts.device)
        name = workspace_name(opts.device)
        if opts.command == "validate":
            print("Valid device " + opts.device + "; shared DroidSpaces is always enabled")
        elif opts.command == "prepare":
            prepare(name, data, dry=opts.dry_run)
        elif opts.command == "toolchain":
            bootstrap_toolchain(name, data, dry=opts.dry_run)
        elif opts.command == "build":
            require(opts.jobs > 0, "jobs must be greater than zero")
            build(name, data, opts.jobs, dry=opts.dry_run)
            if not opts.no_package:
                package(name, data, dry=opts.dry_run)
        elif opts.command == "package":
            package(name, data, dry=opts.dry_run)
        return 0
    except (ForgeError, ModuleError, RootError, subprocess.CalledProcessError, OSError, yaml.YAMLError) as exc:
        print("ERROR:", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
