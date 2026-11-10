"""Prepare the pinned, built-in BakaSU checkout in a managed kernel tree."""

import os
from pathlib import Path
import re
import subprocess


class RootError(Exception):
    """The requested BakaSU checkout cannot be safely integrated."""


def prepare_bakasu(source: Path, spec: dict, run, dry: bool = False) -> str:
    """Install BakaSU without executing upstream installation scripts.

    ``source`` is the caller's managed kernel checkout. Existing KernelSU trees
    must be ordinary Git clones of the configured origin, not worktrees or links.
    The caller owns kernel hook patches and the configuration fragment.
    """
    pin = spec.get("commit", "")
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise RootError("BakaSU requires a full lowercase pinned commit SHA")
    url, ref = spec.get("url"), spec.get("ref")
    if not isinstance(url, str) or not url or url.startswith("-"):
        raise RootError("BakaSU requires a repository URL")
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise RootError("BakaSU requires a branch ref")
    source = Path(source).absolute()
    root = source / "KernelSU"
    drivers = source / "drivers"
    link = drivers / "kernelsu"

    def git(*args, capture=False):
        try:
            return run(["git", "-C", str(root), *args], dry=dry, capture=capture)
        except subprocess.CalledProcessError as exc:
            raise RootError(f"Cannot prepare pinned BakaSU {pin}: {exc}") from exc

    def clone():
        try:
            run(["git", "clone", "--single-branch", "--branch", ref, "--", url, str(root)], dry=dry)
        except subprocess.CalledProcessError as exc:
            raise RootError(f"Cannot clone BakaSU: {exc}") from exc

    if dry:
        if root.exists() or root.is_symlink():
            git("remote", "set-url", "origin", url)
            git("reset", "--hard")
            git("clean", "-fdx")
        else:
            clone()
        git("fetch", "--tags", "origin", ref)
        git("fetch", "origin", pin)
        git("checkout", "--detach", pin)
        git("rev-parse", "HEAD", capture=True)
        print(f"LINK {link} -> ../KernelSU/kernel")
        print(f"INTEGRATE BakaSU in {drivers / 'Makefile'} and {drivers / 'Kconfig'}")
        return pin

    try:
        if drivers.is_symlink() or not drivers.is_dir() or drivers.resolve().parent != source.resolve():
            raise RootError(f"Missing or unsafe kernel drivers directory: {drivers}")
        if link.is_symlink():
            if os.readlink(link) != "../KernelSU/kernel":
                raise RootError(f"Conflicting BakaSU driver symlink: {link}")
        elif link.exists():
            raise RootError(f"Conflicting BakaSU driver path: {link}")

        # Read and validate the host layout before changing either repository.
        makefile = drivers / "Makefile"
        kconfig = drivers / "Kconfig"
        for path in (makefile, kconfig):
            if path.is_symlink() or not path.is_file():
                raise RootError(f"Missing or unsafe kernel integration file: {path}")
        make = makefile.read_text(encoding="utf-8")
        config = kconfig.read_text(encoding="utf-8")
        make_entry = "obj-$(CONFIG_KSU) += kernelsu/"
        config_entry = 'source "drivers/kernelsu/Kconfig"'
        make_matches = re.findall(r"^\s*obj-\$\(CONFIG_KSU\)\s*\+=\s*kernelsu/\s*$", make, re.MULTILINE)
        config_matches = re.findall(r'^\s*source\s+"drivers/kernelsu/Kconfig"\s*$', config, re.MULTILINE)
        if len(make_matches) > 1 or len(config_matches) > 1:
            raise RootError("Duplicate BakaSU driver integration entries")
        if not make_matches and re.search(r"^[^#\n]*(?:CONFIG_KSU|kernelsu/)", make, re.MULTILINE):
            raise RootError("Conflicting BakaSU drivers/Makefile integration")
        if not config_matches and re.search(r'^[^#\n]*kernelsu', config, re.MULTILINE):
            raise RootError("Conflicting BakaSU drivers/Kconfig integration")
        lines = config.splitlines(keepends=True)
        depth = 0
        closing = None
        for index, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r'^menu\s+"', stripped):
                depth += 1
            elif stripped == "endmenu":
                depth -= 1
                if depth < 0:
                    raise RootError("Unknown drivers/Kconfig menu layout")
                if depth == 0:
                    closing = index
        if depth or closing is None or any(line.strip() and not line.lstrip().startswith("#") for line in lines[closing + 1:]):
            raise RootError("Unknown drivers/Kconfig menu layout")
        if not make_matches:
            make = make.rstrip("\n") + "\n" + make_entry + "\n"
        if not config_matches:
            lines.insert(closing, config_entry + "\n\n")
            config = "".join(lines)

        if root.is_symlink():
            raise RootError(f"Refusing symlinked BakaSU checkout: {root}")
        if root.exists():
            if not root.is_dir() or root.resolve().parent != source.resolve():
                raise RootError(f"Conflicting BakaSU checkout path: {root}")
            metadata = root / ".git"
            if metadata.is_symlink() or not metadata.is_dir():
                raise RootError(f"BakaSU requires a plain .git directory: {root}")
            if Path(git("rev-parse", "--show-toplevel", capture=True)).resolve() != root.resolve():
                raise RootError(f"BakaSU checkout escapes its managed path: {root}")
            if git("config", "--get", "remote.origin.url", capture=True) != url:
                raise RootError(f"Conflicting BakaSU origin in {root}")
            git("remote", "set-url", "origin", url)
            git("reset", "--hard")
            git("clean", "-fdx")
            shallow = git("rev-parse", "--is-shallow-repository", capture=True)
            if shallow == "true":
                git("fetch", "--unshallow", "--tags", "origin", ref)
        else:
            clone()
        git("fetch", "--tags", "origin", ref)
        git("fetch", "origin", pin)
        git("checkout", "--detach", pin)
        actual = git("rev-parse", "HEAD", capture=True)
        if actual != pin:
            raise RootError(f"BakaSU checkout SHA mismatch: expected {pin}, got {actual}")
        for name in ("Kconfig", "Kbuild"):
            path = root / "kernel" / name
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise RootError(f"Pinned BakaSU is missing a safe kernel/{name}")
        if not link.is_symlink():
            link.symlink_to("../KernelSU/kernel", target_is_directory=True)
        if makefile.read_text(encoding="utf-8") != make:
            makefile.write_text(make, encoding="utf-8")
        if kconfig.read_text(encoding="utf-8") != config:
            kconfig.write_text(config, encoding="utf-8")
        return actual
    except OSError as exc:
        raise RootError(f"Cannot integrate BakaSU: {exc}") from exc
