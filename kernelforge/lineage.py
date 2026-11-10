"""Provision the pinned official Capri LineageOS originals and Linux image tools.

The managed cache is reusable, but every cached asset is checked before use.
No device is opened or modified. Run with ``python -m kernelforge.lineage``.
"""

import argparse
import base64
import binascii
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import stat
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen


class LineageError(Exception):
    """A pinned LineageOS input could not be safely provisioned."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str
    size: int = None
    gitiles: bool = False


ROM = Asset(
    "lineage-24.0-20261105-nightly-capri-signed.zip",
    "https://mirrorbits.lineageos.org/full/capri/20261105/lineage-24.0-20261105-nightly-capri-signed.zip",
    "34486153eacc2b0e6f6de471a89dae726f278f66a95adb2977a9b952105b3463",
    1147102832,
)
EXTRACTOR_ARCHIVE = Asset(
    "payload-dumper-go_2.1.0_linux_amd64.tar.gz",
    "https://github.com/ssut/payload-dumper-go/releases/download/2.1.0/payload-dumper-go_2.1.0_linux_amd64.tar.gz",
    "bbbb53a71955c69272afdef7bc7e83a1bb1770f453d0c21dcaf61a3ba0463c11",
)
_AOSP = ("https://android.googlesource.com/kernel/prebuilts/build-tools/+/"
         "07b44a63150de430daa6338a18067e7521a458e1/linux-x86/")
TOOLS = (
    Asset("bin/avbtool", _AOSP + "bin/avbtool?format=TEXT",
          "8d481c5cdced74953ca5f3574740137850239c8cfd390d57f9a9fb2d690fb58d", gitiles=True),
    Asset("bin/fec", _AOSP + "bin/fec?format=TEXT",
          "d388dff5eb35564682429e8c1e15b502be7e83972a8f1aa33643c01a5d54a65a", gitiles=True),
    Asset("lib64/libc++.so", _AOSP + "lib64/libc++.so?format=TEXT",
          "87c21d5f283111d47067ccae33a111c88a4c0d96a1b8b35f80ea4103f4af4850", gitiles=True),
)
ORIGINALS = {
    "boot.img": (100663296, "b3ef4289f9384f56507e78d17b9412a398f63528ee53f0ba970b1191dac6b422"),
    "vendor_boot.img": (100663296, "3fa5e623b769ec1df3c500aae531b922bbffe421777336d1252a2dc49e93b381"),
    "vendor.img": (557658112, "295d3a1fab01c3b1df988a70fa7cf6e3b781a84343713245703b5350ee1b0d11"),
}


def _inspect(path, *, directory=False, owned=True):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or (owned and info.st_uid != os.getuid()):
        raise LineageError(f"Unsafe cache {'directory' if directory else 'file'}: {path}")
    if owned and (info.st_mode & 0o022 or (not directory and info.st_nlink != 1)):
        raise LineageError(f"Cache asset is writable by others or hard-linked: {path}")


def _directory(path, *, owned=True):
    """Reject symlinks in every ancestor, without resolving them away."""
    if path.parent != path:
        _directory(path.parent, owned=False)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o755)
    _inspect(path, directory=True, owned=owned)


def _verify(path, digest, size=None):
    _inspect(path)
    if size is not None and path.stat().st_size != size:
        raise LineageError(f"Size mismatch for {path.name}")
    actual = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(chunk)
    if actual.hexdigest() != digest:
        raise LineageError(f"SHA256 mismatch for {path.name}")


def _publish(source, destination):
    # No existing cache entry, including an empty directory, may be replaced.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise LineageError("Cache publication requires Linux renameat2")
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise LineageError(f"Cannot publish {destination.name}: {os.strerror(ctypes.get_errno())}")


def _download(asset, destination, work):
    if destination.exists() or destination.is_symlink():
        _verify(destination, asset.sha256, asset.size)
        return
    staged = work / destination.name
    try:
        with urlopen(asset.url, timeout=120) as response, staged.open("xb") as stream:
            pending = b""
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if asset.gitiles:
                    pending += b"".join(chunk.split())
                    count = len(pending) // 4 * 4
                    chunk = base64.b64decode(pending[:count], validate=True)
                    pending = pending[count:]
                stream.write(chunk)
            if pending:
                stream.write(base64.b64decode(pending, validate=True))
        _verify(staged, asset.sha256, asset.size)
        _publish(staged, destination)
    except (OSError, ValueError, binascii.Error) as exc:
        raise LineageError(f"Cannot download {asset.name}: {exc}") from exc


def _extractor(archive_path, destination, work):
    """Read only the regular pinned executable, never extract archive paths."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            entries = [entry for entry in archive.getmembers() if entry.name == "payload-dumper-go"]
            if len(entries) != 1 or not entries[0].isreg():
                raise LineageError("Invalid payload-dumper-go archive member")
            member = entries[0]
            # Hash the executable from the verified archive, including on cache hits.
            digest = hashlib.sha256()
            source = archive.extractfile(member)
            staged = work / "payload-dumper-go"
            with source, staged.open("xb") as stream:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    stream.write(chunk)
            if destination.exists() or destination.is_symlink():
                _verify(destination, digest.hexdigest(), member.size)
            else:
                _verify(staged, digest.hexdigest(), member.size)
                staged.chmod(0o755)
                _publish(staged, destination)
        destination.chmod(0o755)
    except (OSError, tarfile.TarError) as exc:
        raise LineageError(f"Cannot provision payload-dumper-go: {exc}") from exc


def _originals(directory):
    _inspect(directory, directory=True)
    if {path.name for path in directory.iterdir()} != set(ORIGINALS):
        raise LineageError(f"Incomplete or unexpected original images in {directory}")
    for name, (size, digest) in ORIGINALS.items():
        _verify(directory / name, digest, size)


def prepare(output: Path) -> Path:
    """Return verified originals in a managed cache for the fixed tested ROM.

    Corrupt/unsafe existing entries fail closed and are left untouched. Downloads
    and extraction are staged privately; only verified assets are published.
    """
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise LineageError("Pinned LineageOS tools require a Linux x86_64 host")
    output = Path(os.path.abspath(output))
    try:
        _directory(output)
        for relative in ("tools", "tools/bin", "tools/lib64"):
            _directory(output / relative)
        # Validate all managed directories before any downloads or chmods.
        for relative in ("tools", "tools/bin", "tools/lib64"):
            _inspect(output / relative, directory=True)
        originals = output / "originals"
        if originals.exists() or originals.is_symlink():
            _originals(originals)
        with tempfile.TemporaryDirectory(prefix=".lineage-", dir=output) as temporary:
            work = Path(temporary)
            rom_path = output / ROM.name
            archive_path = output / EXTRACTOR_ARCHIVE.name
            _download(ROM, rom_path, work)
            _download(EXTRACTOR_ARCHIVE, archive_path, work)
            extractor = output / "tools/bin/payload-dumper-go"
            _extractor(archive_path, extractor, work)
            for asset in TOOLS:
                target = output / "tools" / asset.name
                _download(asset, target, work)
                target.chmod(0o755 if target.parent.name == "bin" else 0o644)
            if originals.exists():
                return originals
            staged = work / "originals"
            staged.mkdir(mode=0o700)
            try:
                result = subprocess.run(
                    [str(extractor), "-p", "boot,vendor_boot,vendor", "-o", str(staged), str(rom_path)],
                    capture_output=True,
                )
            except OSError as exc:
                raise LineageError(f"Cannot execute payload-dumper-go: {exc}") from exc
            if result.returncode:
                raise LineageError(f"payload-dumper-go failed ({result.returncode}): "
                                   + result.stderr.decode(errors="replace"))
            _originals(staged)
            _publish(staged, originals)
            return originals
    except (OSError, ValueError) as exc:
        raise LineageError(f"Cannot prepare LineageOS cache {output}: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        originals = prepare(args.output)
    except LineageError as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(originals)


if __name__ == "__main__":
    main()
