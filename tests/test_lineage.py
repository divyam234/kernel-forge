"""Pinned provisioning with local transports and real, tiny extraction processes."""

import base64
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from kernelforge import lineage


def sha(data):
    return hashlib.sha256(data).hexdigest()


class PrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.images = {"boot.img": b"boot image", "vendor_boot.img": b"vendor boot image",
                       "vendor.img": b"vendor image"}
        self.source = self.root / "official.zip"
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, data in self.images.items():
                archive.writestr(name, data)
        rom = lineage.Asset("official.zip", self.source.as_uri(), sha(self.source.read_bytes()),
                            self.source.stat().st_size)
        self.patch("ROM", rom)
        self.patch("ORIGINALS", {name: (len(data), sha(data)) for name, data in self.images.items()})
        self.patch("TOOLS", tuple(self.tool(name, b"native " + name.encode())
                                   for name in ("bin/avbtool", "bin/fec", "lib64/libc++.so")))
        self.install_extractor()
        for name, value in (("system", "Linux"), ("machine", "x86_64")):
            mocked = patch.object(lineage.platform, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def patch(self, name, value):
        mocked = patch.object(lineage, name, value)
        mocked.start()
        self.addCleanup(mocked.stop)

    def tool(self, name, data):
        source = self.root / (Path(name).name + ".base64")
        source.write_bytes(base64.b64encode(data))
        return lineage.Asset(name, source.as_uri(), sha(data), len(data), True)

    def install_extractor(self, action=None, *, symlink=False):
        if action is None:
            action = ("with zipfile.ZipFile(sys.argv[5]) as archive:\n"
                      "    for name in ('boot.img', 'vendor_boot.img', 'vendor.img'):\n"
                      "        (Path(sys.argv[4]) / name).write_bytes(archive.read(name))\n")
        # Enforce exact partition selection, an explicit ZIP path, and default
        # verification in the executable rather than recording mocked calls.
        script = (f"#!{sys.executable}\n"
                  "import sys, zipfile\nfrom pathlib import Path\n"
                  "assert len(sys.argv) == 6\n"
                  "assert sys.argv[1:4] == ['-p', 'boot,vendor_boot,vendor', '-o']\n"
                  "assert sys.argv[4] == str(Path(sys.argv[4]).absolute())\n"
                  "assert sys.argv[5] == str(Path(sys.argv[5]).absolute())\n")
        script += action
        data = script.encode()
        source = self.root / "extractor.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            entry = tarfile.TarInfo("payload-dumper-go")
            entry.size = len(data)
            if symlink:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/bin/false"
                entry.size = 0
            archive.addfile(entry, None if symlink else io.BytesIO(data))
        self.patch("EXTRACTOR_ARCHIVE", lineage.Asset(
            "extractor.tar.gz", source.as_uri(), sha(source.read_bytes()), source.stat().st_size))

    def assert_unpublished(self):
        self.assertFalse((self.cache / "originals").exists())
        self.assertFalse(list(self.cache.glob(".lineage-*")))

    def test_verified_decoded_assets_and_exact_images_are_published(self):
        originals = lineage.prepare(self.cache)
        self.assertEqual(originals, self.cache / "originals")
        self.assertEqual({p.name: p.read_bytes() for p in originals.iterdir()}, self.images)
        for asset in lineage.TOOLS:
            path = self.cache / "tools" / asset.name
            self.assertEqual(sha(path.read_bytes()), asset.sha256)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),
                             0o755 if path.parent.name == "bin" else 0o644)
        self.assertEqual(stat.S_IMODE((self.cache / "tools/bin/payload-dumper-go").stat().st_mode), 0o755)
        self.assertFalse(list(self.cache.glob(".lineage-*")))

    def test_valid_cache_is_rehashed_and_reused_without_transport_or_execution(self):
        originals = lineage.prepare(self.cache)
        before = {p: p.stat().st_mtime_ns for p in originals.iterdir()}
        with patch.object(lineage, "urlopen", side_effect=AssertionError("unexpected download")), \
                patch.object(lineage.subprocess, "run", side_effect=AssertionError("unexpected extraction")):
            self.assertEqual(lineage.prepare(self.cache), originals)
        self.assertEqual({p: p.stat().st_mtime_ns for p in originals.iterdir()}, before)

    def test_download_checksum_and_size_failures_never_publish(self):
        for change in ({"sha256": "0" * 64}, {"size": lineage.ROM.size + 1}):
            with self.subTest(change=change), patch.object(lineage, "ROM", replace(lineage.ROM, **change)):
                with self.assertRaises(lineage.LineageError):
                    lineage.prepare(self.cache)
                self.assertFalse((self.cache / lineage.ROM.name).exists())
                self.assert_unpublished()

    def test_corrupt_cache_is_rejected_without_repair_or_unrelated_deletion(self):
        lineage.prepare(self.cache)
        marker = self.cache / "unrelated"
        marker.write_bytes(b"keep")
        targets = [self.cache / lineage.ROM.name, self.cache / "extractor.tar.gz",
                   self.cache / "tools/bin/avbtool", self.cache / "tools/bin/payload-dumper-go",
                   self.cache / "originals/vendor.img"]
        for target in targets:
            with self.subTest(target=target):
                original = target.read_bytes()
                target.write_bytes(b"corrupt")
                with self.assertRaises(lineage.LineageError):
                    lineage.prepare(self.cache)
                self.assertEqual(target.read_bytes(), b"corrupt")
                self.assertEqual(marker.read_bytes(), b"keep")
                target.write_bytes(original)

    def test_failed_partial_extraction_never_publishes_originals(self):
        self.install_extractor("(Path(sys.argv[4]) / 'boot.img').write_bytes(b'partial')\nsys.exit(3)\n")
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assert_unpublished()

    def test_successful_exit_with_incomplete_images_is_not_published(self):
        self.install_extractor("(Path(sys.argv[4]) / 'boot.img').write_bytes(b'boot image')\n")
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assert_unpublished()

    def test_extracted_symlink_is_not_published_or_followed(self):
        outside = self.root / "outside-image"
        outside.write_bytes(self.images["boot.img"])
        self.install_extractor(
            "with zipfile.ZipFile(sys.argv[5]) as archive:\n"
            "    for name in ('vendor_boot.img', 'vendor.img'):\n"
            "        (Path(sys.argv[4]) / name).write_bytes(archive.read(name))\n"
            f"(Path(sys.argv[4]) / 'boot.img').symlink_to({str(outside)!r})\n")
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assertEqual(outside.read_bytes(), self.images["boot.img"])
        self.assert_unpublished()

    def test_wrong_extracted_hash_or_size_never_publishes_originals(self):
        for image in (b"x" * len(self.images["boot.img"]), b"short"):
            with self.subTest(image=image):
                self.images["boot.img"] = image
                with zipfile.ZipFile(self.source, "w") as archive:
                    for name, data in self.images.items():
                        archive.writestr(name, data)
                self.patch("ROM", replace(lineage.ROM, sha256=sha(self.source.read_bytes()),
                                          size=self.source.stat().st_size))
                # Remove only this test's previously verified ROM between cases.
                cached = self.cache / lineage.ROM.name
                if cached.exists():
                    cached.unlink()
                with self.assertRaises(lineage.LineageError):
                    lineage.prepare(self.cache)
                self.assert_unpublished()

    def test_symlinked_output_or_ancestor_never_writes_through(self):
        destination = self.root / "destination"
        destination.mkdir()
        self.cache.symlink_to(destination, target_is_directory=True)
        for output in (self.cache, self.cache / "nested"):
            with self.subTest(output=output), self.assertRaises(lineage.LineageError):
                lineage.prepare(output)
        self.assertEqual(list(destination.iterdir()), [])

    def test_symlinked_cached_assets_and_directories_are_not_mutated(self):
        lineage.prepare(self.cache)
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        target = self.cache / "tools/bin/fec"
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_symlinked_tools_directory_never_writes_through(self):
        self.cache.mkdir()
        (self.cache / "tools").mkdir()
        outside = self.root / "outside-tools"
        outside.mkdir()
        (self.cache / "tools/bin").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unowned_cache_is_rejected_before_mutation(self):
        self.cache.mkdir()
        with patch.object(lineage.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(lineage.LineageError):
                lineage.prepare(self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_shared_writable_cache_and_hardlinked_assets_are_rejected(self):
        self.cache.mkdir()
        self.cache.chmod(0o777)
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.cache.chmod(0o755)
        lineage.prepare(self.cache)
        asset = self.cache / "tools/bin/avbtool"
        os.link(asset, self.root / "hardlink")
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)

    def test_nonregular_archive_executable_is_rejected(self):
        self.install_extractor(symlink=True)
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assertFalse((self.cache / "tools/bin/payload-dumper-go").exists())
        self.assert_unpublished()

    def test_invalid_gitiles_bytes_never_publish_tool(self):
        asset = lineage.TOOLS[0]
        (self.root / "avbtool.base64").write_bytes(b"not base64!")
        with self.assertRaises(lineage.LineageError):
            lineage.prepare(self.cache)
        self.assertFalse((self.cache / "tools" / asset.name).exists())
        self.assert_unpublished()

    def test_unsupported_host_fails_without_creating_cache(self):
        for system, machine in (("Darwin", "x86_64"), ("Linux", "aarch64")):
            with self.subTest(system=system, machine=machine), \
                    patch.object(lineage.platform, "system", return_value=system), \
                    patch.object(lineage.platform, "machine", return_value=machine):
                with self.assertRaises(lineage.LineageError):
                    lineage.prepare(self.cache)
                self.assertFalse(self.cache.exists())

    def test_cli_failure_has_no_traceback(self):
        stderr = io.StringIO()
        with patch.object(lineage.platform, "machine", return_value="aarch64"), \
                patch("sys.stderr", stderr), self.assertRaises(SystemExit) as failure:
            lineage.main(["--output", str(self.cache)])
        self.assertEqual(failure.exception.code, 1)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
