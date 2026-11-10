"""Validate and stage the modules built alongside a packaged kernel."""

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile


class ModuleError(Exception):
    """The kernel and its installable modules do not form a coherent set."""


def _name(value: str) -> str:
    return value.replace("-", "_")


def _filename(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_+-]+\.ko", value))


@dataclass(frozen=True)
class _Module:
    name: str
    vermagic: str
    depends: tuple[str, ...]
    aliases: tuple[str, ...]
    softdeps: tuple[str, ...]
    versions: tuple[tuple[str, int], ...]
    required: frozenset[str]


def _parse(path: Path) -> _Module:
    try:
        with path.open("rb") as stream:
            elf = ELFFile(stream)
            if elf.elfclass != 64 or not elf.little_endian or elf["e_machine"] != "EM_AARCH64" or elf["e_type"] != "ET_REL":
                raise ModuleError(f"{path}: expected a little-endian ARM64 relocatable module")
            info = elf.get_section_by_name(".modinfo")
            versions = elf.get_section_by_name("__versions")
            symbols = elf.get_section_by_name(".symtab")
            if info is None or versions is None or symbols is None:
                raise ModuleError(f"{path}: missing .modinfo, __versions or .symtab")
            fields: dict[str, list[str]] = {}
            for item in info.data().rstrip(b"\0").split(b"\0"):
                if not item:
                    continue
                key, sep, value = item.decode("utf-8").partition("=")
                if not sep or "\n" in value or "\r" in value:
                    raise ModuleError(f"{path}: malformed .modinfo")
                fields.setdefault(key, []).append(value)
            for key in ("name", "vermagic"):
                if len(fields.get(key, [])) != 1 or not fields[key][0].strip():
                    raise ModuleError(f"{path}: missing or ambiguous {key}")
            name = fields["name"][0]
            if not re.fullmatch(r"[A-Za-z0-9_+-]+", name):
                raise ModuleError(f"{path}: invalid internal module name {name!r}")
            raw = versions.data()
            if not raw or len(raw) % 64:
                raise ModuleError(f"{path}: malformed ARM64 __versions records")
            crcs = {}
            for offset in range(0, len(raw), 64):
                symbol = raw[offset + 8:offset + 64].split(b"\0", 1)[0].decode("ascii")
                if not symbol or symbol in crcs:
                    raise ModuleError(f"{path}: empty or duplicate versioned symbol {symbol!r}")
                crcs[symbol] = int.from_bytes(raw[offset:offset + 8], "little")
            required = frozenset(s.name for s in symbols.iter_symbols()
                                 if s.name and s["st_shndx"] == "SHN_UNDEF"
                                 and s["st_info"]["bind"] != "STB_WEAK"
                                 and s["st_info"]["bind"] != "STB_LOCAL")
            depends = tuple(n for value in fields.get("depends", []) for n in value.split(",") if n)
            return _Module(name, fields["vermagic"][0], depends,
                           tuple(fields.get("alias", [])), tuple(fields.get("softdep", [])),
                           tuple(sorted(crcs.items())), required)
    except (OSError, ELFError, UnicodeError, ValueError, KeyError) as exc:
        raise ModuleError(f"{path}: cannot read module: {exc}") from exc


def _symvers(path: Path) -> dict[str, int]:
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) < 3:
            raise ModuleError(f"{path}: malformed symbol record {line!r}")
        try:
            crc = int(fields[0], 16)
        except ValueError as exc:
            raise ModuleError(f"{path}: invalid CRC {fields[0]!r}") from exc
        if fields[1] in result:
            raise ModuleError(f"{path}: duplicate exported symbol {fields[1]}")
        result[fields[1]] = crc
    return result


def _manifest(directory: Path, destination: Path) -> None:
    destination.write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(directory.iterdir()) if path.is_file() and path != destination
    ))


def _module_paths(out: Path) -> list[Path]:
    """Resolve build paths, including Linux 4.19's installation-only kernel/ prefix."""
    paths = []
    for item in (out / "modules.order").read_text().splitlines():
        relative = PurePosixPath(item)
        if not item or relative.is_absolute() or ".." in relative.parts or relative.suffix not in (".ko", ".o"):
            raise ModuleError(f"modules.order: unsafe or unsupported module path {item!r}")
        relative = relative.with_suffix(".ko")
        candidates = [out.joinpath(*relative.parts)]
        if relative.parts[0] == "kernel":
            candidates.append(out.joinpath(*relative.parts[1:]))
        resolved = set()
        for candidate in candidates:
            path = candidate.resolve()
            if not path.is_relative_to(out):
                raise ModuleError(f"modules.order: escaping path: {item}")
            if path.is_file():
                resolved.add(path)
        if len(resolved) != 1:
            raise ModuleError(f"modules.order: missing module or ambiguous build path: {item}")
        paths.append(resolved.pop())
    if not paths:
        raise ModuleError("modules.order contains no modules")
    return paths


def archive_modules(out: Path, destination: Path) -> None:
    """Retain repackable build inputs using the same path rules as staging."""
    out = out.resolve()
    paths = _module_paths(out)
    for name in ("modules.order", "Module.symvers", "include/config/kernel.release"):
        path = (out / name).resolve()
        if not path.is_relative_to(out) or not path.is_file():
            raise ModuleError(f"Missing or escaping module build input: {name}")
        paths.append(path)
    with tarfile.open(destination, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(out), recursive=False)


def stage_modules(out: Path, staging: Path, cfg: dict, strip_tool: Path | None = None) -> None:
    """Stage a CRC-checked module set and its explicitly ordered early subset.

    ``cfg`` is the package.modules mapping. Source files are never modified.
    Missing build metadata, unresolved dependencies and ambiguous names fail closed.
    """
    try:
        _stage_modules(out.resolve(), staging, cfg, strip_tool)
    except OSError as exc:
        raise ModuleError(f"Cannot stage kernel modules: {exc}") from exc


def _stage_modules(out: Path, staging: Path, cfg: dict, strip_tool: Path | None) -> None:
    release = (out / "include/config/kernel.release").read_text().strip()
    if not release or len(release.split()) != 1:
        raise ModuleError("include/config/kernel.release must contain exactly one kernel release")
    exports = _symvers(out / "Module.symvers")
    sources: dict[str, Path] = {}
    modules: dict[str, _Module] = {}
    for path in _module_paths(out):
        base = path.name
        if not _filename(base) or base in sources:
            raise ModuleError(f"modules.order: invalid or duplicate module basename {base!r}")
        module = _parse(path)
        if module.vermagic.split()[0] != release:
            raise ModuleError(f"{base}: vermagic release {module.vermagic!r} does not match {release}")
        versions = dict(module.versions)
        missing = (module.required | {"module_layout"}) - versions.keys()
        if missing:
            raise ModuleError(f"{base}: missing imported CRCs: {', '.join(sorted(missing))}")
        for symbol, crc in versions.items():
            if symbol not in exports or exports[symbol] != crc:
                expected = f"0x{exports[symbol]:08x}" if symbol in exports else "absent"
                raise ModuleError(f"{base}: CRC mismatch for {symbol}: module 0x{crc:08x}, Module.symvers {expected}")
        sources[base], modules[base] = path, module
    if not modules:
        raise ModuleError("modules.order contains no modules")
    aliases = cfg.get("aliases", {})
    if not isinstance(aliases, dict) or any(k not in modules or not isinstance(v, str) or not _filename(v)
                                            for k, v in aliases.items()):
        raise ModuleError("modules aliases must map built module basenames to installed .ko basenames")
    installed = {base: aliases.get(base, base) for base in modules}
    if len({_name(v[:-3]) for v in installed.values()}) != len(installed):
        raise ModuleError("module aliases cause installed destination collisions")
    names = {}
    for base, module in modules.items():
        key = _name(module.name)
        if key in names:
            raise ModuleError(f"duplicate internal module name: {module.name}")
        names[key] = base
    deps = {}
    for base, module in modules.items():
        deps[base] = []
        for name in module.depends:
            if _name(name) not in names:
                raise ModuleError(f"{base}: dependency {name!r} is not in modules.order")
            dependency = names[_name(name)]
            if dependency not in deps[base]:
                deps[base].append(dependency)

    def closure(base: str, visiting: tuple[str, ...] = ()) -> list[str]:
        if base in visiting:
            raise ModuleError(f"module dependency cycle: {' -> '.join((*visiting, base))}")
        # libmodprobe walks the dependency list backwards: dependents precede
        # their prerequisites, including shared prerequisites in a diamond.
        result = []
        for dependency in deps[base]:
            for entry in [dependency, *closure(dependency, (*visiting, base))]:
                if entry in result:
                    result.remove(entry)
                result.append(entry)
        return result

    closures = {base: closure(base) for base in modules}
    early = cfg.get("vendor_boot", [])
    if not isinstance(early, list) or any(not isinstance(b, str) or b not in modules for b in early) or len(set(early)) != len(early):
        raise ModuleError("vendor_boot must be an ordered list of unique built module basenames")
    for base in early:
        if _name(modules[base].name) != _name(installed[base][:-3]):
            raise ModuleError(f"vendor_boot {base}: installed filename must match its internal module name")
        missing = set(closures[base]) - set(early)
        if missing:
            raise ModuleError(f"vendor_boot {base}: early dependency outside configured subset: {', '.join(sorted(missing))}")
        if any(early.index(dep) > early.index(base) for dep in closures[base]):
            raise ModuleError(f"vendor_boot {base}: dependencies must precede their consumer in load order")

    def metadata(directory: Path, subset: list[str]) -> None:
        (directory / "modules.dep").write_text("".join(
            f"{installed[b]}:{''.join(' ' + installed[d] for d in closures[b])}\n" for b in subset))
        (directory / "modules.alias").write_text("".join(
            f"alias {alias} {_name(installed[b][:-3])}\n" for b in subset for alias in modules[b].aliases))
        softdeps = []
        for base in subset:
            for value in modules[base].softdeps:
                words = value.split()
                if not words or words[0] not in ("pre:", "post:"):
                    raise ModuleError(f"{base}: malformed softdep {value!r}")
                rewritten = [(_name(installed[names[_name(w)]][:-3]) if _name(w) in names else w) for w in words]
                softdeps.append(f"softdep {_name(installed[base][:-3])} {' '.join(rewritten)}\n")
        (directory / "modules.softdep").write_text("".join(softdeps))

    vendor = staging / "module-files/vendor/lib/modules"
    early_dir = staging / "vendor_boot-files/ramdisk/lib/modules"
    for directory in (vendor, early_dir):
        if directory.exists():
            raise ModuleError(f"Refusing to overwrite existing module staging directory: {directory}")
    vendor.mkdir(parents=True)
    early_dir.mkdir(parents=True)
    for base in modules:
        destination = vendor / installed[base]
        if strip_tool and sources[base].read_bytes().endswith(b"~Module signature appended~\n"):
            raise ModuleError(f"{base}: cannot strip a signed module without invalidating its signature")
        shutil.copyfile(sources[base], destination)
        if strip_tool:
            try:
                subprocess.run([str(strip_tool), "--strip-debug", str(destination)], check=True,
                               capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
                raise ModuleError(f"{base}: llvm-strip failed: {detail}") from exc
            if _parse(destination) != modules[base]:
                raise ModuleError(f"{base}: llvm-strip changed module loading metadata")
    metadata(vendor, sorted(modules))
    for base in early:
        shutil.copyfile(vendor / installed[base], early_dir / installed[base])
    metadata(early_dir, early)
    (early_dir / "modules.load").write_text("".join(f"{modules[b].name}\n" for b in early))
    _manifest(vendor, vendor / "modules.sha256")
    _manifest(early_dir, staging / "module-files/early.sha256")
