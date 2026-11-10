"""YAML-driven Android kernel builder. All modified Git trees live under .work/."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work"
CONFIG = ROOT / "kernel.yaml"
WORK_NAME = "kernel"
SHA = re.compile(r"^[a-f0-9]{40}$")
SUPPORTED = {"apiVersion", "source", "toolchain", "build", "droidspaces", "overlays", "patches", "package"}
LLVM_TOOLS = ("clang", "clang++", "ld.lld", "llvm-ar", "llvm-nm",
              "llvm-objcopy", "llvm-objdump", "llvm-readelf", "llvm-size", "llvm-strip")


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


def load_config():
    require(CONFIG.is_file(), "Missing kernel.yaml")
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fields(data, SUPPORTED, "kernel.yaml")
    require(data.get("apiVersion") == "kernelforge/v1", "Unsupported apiVersion")
    # DroidSpaces is mandatory and has no enabled/disabled switch.
    droidspaces = data.get("droidspaces")
    fields(droidspaces, {"config", "destination", "target"}, "droidspaces")
    local_path(droidspaces.get("config"), "droidspaces.config")
    relative(droidspaces.get("destination"), "droidspaces.destination")
    relative(droidspaces.get("target"), "droidspaces.target")

    src = data.get("source")
    fields(src, {"url", "ref", "commit"}, "source")
    require(isinstance(src.get("url"), str) and src["url"].startswith("https://"),
            "source.url must be an HTTPS Git URL")
    require(isinstance(src.get("ref"), str) and bool(src["ref"]), "source.ref is required")
    if src.get("commit") is not None:
        require(isinstance(src["commit"], str) and bool(SHA.fullmatch(src["commit"])),
                "source.commit must be a full 40-character SHA")

    tc = data.get("toolchain")
    fields(tc, {"url", "ref", "commit"}, "toolchain")
    require(isinstance(tc.get("url"), str) and tc["url"].startswith("https://"),
            "toolchain.url must be HTTPS")
    require(isinstance(tc.get("ref"), str) and bool(tc["ref"]),
            "toolchain.ref is required")
    if tc.get("commit") is not None:
        require(isinstance(tc["commit"], str) and bool(SHA.fullmatch(tc["commit"])),
                "toolchain.commit must be a full 40-character SHA")

    build = data.get("build")
    fields(build, {"arch", "defconfig", "fragments", "targets", "artifacts", "strict_config", "ccache"}, "build")
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
    require(type(build.get("strict_config", True)) is bool, "strict_config must be boolean")
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

    package = data.get("package", {"enabled": False})
    validate_item(package, {"enabled", "template", "images"}, "package")
    if package.get("enabled", False):
        local_path(package.get("template"), "package.template")
        require(isinstance(package.get("images"), dict) and bool(package["images"]),
                "package.images must map kernel output paths to names in ZIP")
        for src_image, dst_image in package["images"].items():
            relative(src_image, "package image source")
            relative(dst_image, "package image destination")
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
    commit = data["source"].get("commit")
    if commit:
        # GitHub accepts fetch-by-SHA for reachable commits, including pinned old heads.
        run(["git", "-C", str(source), "fetch", "--depth=1", "origin", commit], dry=dry)
    else:
        commit = run(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"], dry=dry, capture=True)
    run(["git", "-C", str(source), "checkout", "--detach", commit or "FETCH_HEAD"], dry=dry)
    return source, commit


def prepare(name, data, dry=False):
    source, commit = fetch_source(name, data, dry)
    for entry in data.get("patches", []):
        if not entry.get("enabled", True):
            continue
        patch = local_path(entry["file"], "patch.file")
        require(patch.is_file() or dry, "Missing patch: " + str(patch))
        run(["git", "-C", str(source), "apply", "--check", str(patch)], dry=dry)
        run(["git", "-C", str(source), "apply", str(patch)], dry=dry)
    mandatory = {"from": data["droidspaces"]["config"],
                 "to": data["droidspaces"]["destination"], "enabled": True}
    for entry in [mandatory] + data.get("overlays", []):
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
                 "commit": commit, "patches": [p["file"] for p in data.get("patches", [])
                                              if p.get("enabled", True)]}
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
        dest = WORK / "_toolchains" / name / "llvm"
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


def requested_settings(data):
    settings = {}
    mandatory = {"from": data["droidspaces"]["config"],
                 "to": data["droidspaces"]["destination"], "enabled": True}
    for entry in [mandatory] + data.get("overlays", []):
        if entry.get("enabled", True) and entry["to"].endswith(".config"):
            path = local_path(entry["from"], "overlay.from")
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", line)
                if match:
                    settings[match.group(1)] = match.group(2)
                match = re.match(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$", line)
                if match:
                    settings[match.group(1)] = "n"
    return settings


def verify_config(profile, data, dry=False):
    if dry:
        print("VERIFY generated .config against enabled overlay fragments")
        return []
    config = WORK / profile / "out" / ".config"
    require(config.is_file(), "Kernel did not generate .config")
    generated = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        matched = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", line)
        if matched:
            generated[matched.group(1)] = matched.group(2)
        else:
            matched = re.match(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$", line)
            if matched:
                generated[matched.group(1)] = "n"
    mismatches = [(key, want, generated.get(key, "absent"))
                  for key, want in requested_settings(data).items()
                  if generated.get(key, "absent") != want]
    for key, want, got in mismatches:
        print("CONFIG MISMATCH:", key, "requested", want, "got", got, file=sys.stderr)
    return mismatches


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
    run(base + config_targets, cwd=src, env=env, dry=dry)
    mismatch = verify_config(name, data, dry=dry)
    if mismatch and data["build"].get("strict_config", True):
        raise ForgeError("Kernel Kconfig discarded requested features; refusing build")
    run(base + ["-j" + str(jobs)] + data["build"].get("targets", []),
        cwd=src, env=env, dry=dry)
    if data["build"].get("ccache", {}).get("enabled", False):
        run(["ccache", "--show-stats"], env=env, dry=dry)
    if not dry:
        for item in data["build"].get("artifacts", []):
            artifact = (out / relative(item, "build.artifacts entry")).resolve()
            require(artifact.is_relative_to(out.resolve()) and artifact.is_file(),
                    "Required artifact missing after build: " + str(artifact))
    print("Build output:", out)
    return out


def package(name, data, dry=False):
    cfg = data.get("package", {"enabled": False})
    require(cfg.get("enabled") is True, "Packaging disabled. Configure a device-specific AnyKernel3 template.")
    folder = managed(name, dry)
    template = local_path(cfg["template"], "package.template")
    script = template / "anykernel.sh"
    require(script.is_file() or dry, "Template must contain anykernel.sh")
    if not dry:
        body = script.read_text(encoding="utf-8")
        require(bool(re.search(r"^\s*do\.devicecheck=1\s*;?", body, re.M)),
                "Refusing to package: AnyKernel3 device check must be enabled")
    staging = folder / "package-staging"
    out = folder / "out" / "arch" / data["build"]["arch"] / "boot"
    artifact = folder / "kernel.zip"
    print("PACKAGE", template, "->", artifact)
    if dry:
        return artifact
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(template, staging, symlinks=False)
    for src_image, dst_image in cfg["images"].items():
        source = (out / relative(src_image, "package image source")).resolve()
        require(source.is_relative_to(out.resolve()) and source.is_file(), "Missing kernel artifact: " + str(source))
        dest = (staging / relative(dst_image, "package image destination")).resolve()
        require(dest.is_relative_to(staging.resolve()), "Unsafe ZIP destination")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(staging.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(staging))
    shutil.rmtree(staging)
    print("Created:", artifact)
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description="KernelForge: single kernel.yaml with mandatory DroidSpaces")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "prepare", "toolchain", "build", "package"):
        sub = subs.add_parser(command)
        if command in ("prepare", "toolchain", "build", "package"):
            sub.add_argument("--dry-run", action="store_true", help="Print actions without writing or downloading")
        if command == "build":
            sub.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    opts = parser.parse_args(argv)
    try:
        data = load_config()
        if opts.command == "validate":
            print("Valid kernel.yaml; DroidSpaces is always enabled")
        elif opts.command == "prepare":
            prepare(WORK_NAME, data, dry=opts.dry_run)
        elif opts.command == "toolchain":
            bootstrap_toolchain(WORK_NAME, data, dry=opts.dry_run)
        elif opts.command == "build":
            require(opts.jobs > 0, "jobs must be greater than zero")
            build(WORK_NAME, data, opts.jobs, dry=opts.dry_run)
        elif opts.command == "package":
            package(WORK_NAME, data, dry=opts.dry_run)
        return 0
    except (ForgeError, subprocess.CalledProcessError, OSError, yaml.YAMLError) as exc:
        print("ERROR:", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
