"""Behavioral boundaries for the offline image consumer; no device access."""

import hashlib
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
import warnings
import zipfile

from kernelforge.images import ImageError, _extract, _header, _vendor, build_images


def payload_files():
    return {
        "boot-files/Image": b"kernel",
        "tools/magiskboot": b"tool",
        "tools/fec": b"tool",
        "module-files/early.sha256": b"manifest",
        "module-files/vendor/lib/modules/modules.sha256": b"manifest",
    }


def archive(path, files, *, manifest=None, extra=()):
    if manifest is None:
        manifest = {"format": "kernelforge-capri-payload-v1", "device": "capri",
                    "kernel_release": "4.19-capri",
                    "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("payload.json", json.dumps(manifest))
        for name, data in files.items():
            out.writestr(name, data)
        for name, data in extra:
            out.writestr(name, data)


class BootHeaderTests(unittest.TestCase):
    def test_repacked_sizes_may_change_but_os_metadata_must_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original.img"
            repacked = Path(tmp) / "repacked.img"
            data = bytearray(4096)
            data[:8] = b"ANDROID!"
            struct.pack_into("<I", data, 40, 3)
            struct.pack_into("<II", data, 8, 1024, 2048)
            original.write_bytes(data)
            struct.pack_into("<II", data, 8, 3072, 1792)
            repacked.write_bytes(data)
            self.assertEqual(_header(original), _header(repacked))
            struct.pack_into("<I", data, 16, 1)
            repacked.write_bytes(data)
            self.assertNotEqual(_header(original), _header(repacked))


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zip = self.root / "payload.zip"
        self.extracted = self.root / "private"
        self.extracted.mkdir()

    def test_exhaustive_payload_extracts_verified_bytes(self):
        files = payload_files()
        archive(self.zip, files)
        _extract(self.zip, self.extracted)
        self.assertEqual({str(p.relative_to(self.extracted)): p.read_bytes()
                          for p in self.extracted.rglob("*") if p.is_file()}, files)
        self.assertTrue((self.extracted / "tools/magiskboot").stat().st_mode & stat.S_IXUSR)

    def test_unlisted_and_missing_members_are_rejected(self):
        for extra, missing in (([("extra", b"unlisted")], False), ([], True)):
            with self.subTest(missing=missing):
                files = payload_files()
                manifest = {"format": "kernelforge-capri-payload-v1", "device": "capri",
                            "kernel_release": "4.19-capri",
                            "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
                if missing:
                    del files["tools/fec"]
                archive(self.zip, files, manifest=manifest, extra=extra)
                with self.assertRaises(ImageError):
                    _extract(self.zip, self.extracted)

    def test_corrupt_member_is_rejected(self):
        files = payload_files()
        manifest = {"format": "kernelforge-capri-payload-v1", "device": "capri",
                    "kernel_release": "4.19-capri",
                    "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
        files["boot-files/Image"] = b"corrupt"
        archive(self.zip, files, manifest=manifest)
        with self.assertRaises(ImageError):
            _extract(self.zip, self.extracted)

    def test_escaping_and_symlink_members_never_escape_workspace(self):
        for name in ("../escaped", "/absolute", "a/../../escaped", "a\\escaped", "a//escaped"):
            with self.subTest(name=name):
                archive(self.zip, payload_files(), extra=[(name, b"bad")])
                with self.assertRaises(ImageError):
                    _extract(self.zip, self.extracted)
        entry = zipfile.ZipInfo("tools/link")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive(self.zip, payload_files())
        with zipfile.ZipFile(self.zip, "a") as out:
            out.writestr(entry, "../../escaped")
        with self.assertRaises(ImageError):
            _extract(self.zip, self.extracted)
        self.assertFalse((self.root / "escaped").exists())

    def test_duplicate_members_and_json_keys_are_rejected(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive(self.zip, payload_files(), extra=[("tools/fec", b"duplicate")])
        with self.assertRaises(ImageError):
            _extract(self.zip, self.extracted)
        with zipfile.ZipFile(self.zip, "w") as out:
            out.writestr("payload.json", '{"files":{},"files":{}}')
        with self.assertRaises(ImageError):
            _extract(self.zip, self.extracted)

    def test_invalid_manifest_types_and_legacy_packages_are_rejected(self):
        for manifest in ([], None, {}, {"format": "old-installer"}):
            with self.subTest(manifest=manifest):
                with zipfile.ZipFile(self.zip, "w") as out:
                    out.writestr("payload.json", json.dumps(manifest))
                with self.assertRaises(ImageError):
                    _extract(self.zip, self.extracted)

    def test_failures_never_publish_or_modify_inputs_or_existing_output(self):
        self.zip.write_bytes(b"not a zip")
        originals = []
        for name in ("boot", "vendor_boot", "vendor"):
            path = self.root / (name + ".img")
            path.write_bytes(name.encode())
            originals.append(path)
        before = {p: p.read_bytes() for p in [self.zip, *originals]}
        output = self.root / "images"
        with self.assertRaises(ImageError):
            build_images(self.zip, *originals, output)
        self.assertFalse(output.exists())
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        output.mkdir()
        marker = output / "keep"
        marker.write_bytes(b"untouched")
        with self.assertRaises(ImageError):
            build_images(self.zip, *originals, output)
        self.assertEqual(marker.read_bytes(), b"untouched")


@unittest.skipUnless(all(shutil.which(x) for x in ("mke2fs", "debugfs", "e2fsck")),
                     "real ext4 tooling is required")
class VendorFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tree = self.root / "tree"
        modules = tree / "lib/modules"
        modules.mkdir(parents=True)
        (modules / "sample.ko").write_bytes(b"old module" * 4096)
        (modules / "modules.load").write_bytes(b"# original order\nsample\n")
        (tree / "unrelated").write_bytes(b"leave this alone")
        self.image = self.root / "vendor.img"
        with self.image.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024)
        self.command(["mke2fs", "-q", "-t", "ext4", "-F", "-d", tree, self.image])
        self.context = b"u:object_r:vendor_file:s0\0"
        context_file = self.root / "context"
        context_file.write_bytes(self.context)
        for name in ("/lib/modules", "/lib/modules/sample.ko"):
            self.debug(f'ea_set -f "{context_file}" {name} security.selinux', write=True)
        self.debug("sif /lib/modules gid 2000", write=True)
        self.debug("sif /lib/modules/sample.ko uid 123", write=True)
        self.debug("sif /lib/modules/sample.ko gid 456", write=True)
        self.debug("sif /lib/modules/sample.ko mode 0100640", write=True)
        self.replacements = self.root / "replacement"
        self.replacements.mkdir()
        (self.replacements / "sample.ko").write_bytes(b"new module" * 1024)
        (self.replacements / "modules.dep").write_bytes(b"sample.ko:\n")

    def command(self, args):
        return subprocess.run([str(x) for x in args], capture_output=True, check=True)

    def debug(self, command, write=False):
        return self.command(["debugfs", *(["-w"] if write else []), "-R", command, self.image])

    def read_file(self, name):
        target = self.root / "readback"
        target.unlink(missing_ok=True)
        self.debug(f'dump {name} "{target}"')
        return target.read_bytes()

    def test_real_ext4_replacement_preserves_metadata_load_order_and_other_files(self):
        _vendor(self.image, self.replacements, self.root / "work")
        for path in self.replacements.iterdir():
            self.assertEqual(self.read_file("/lib/modules/" + path.name), path.read_bytes())
        self.assertEqual(self.read_file("/lib/modules/modules.load"), b"# original order\nsample\n")
        self.assertEqual(self.read_file("/unrelated"), b"leave this alone")
        attrs = self.debug("stat /lib/modules/sample.ko").stdout.decode()
        self.assertRegex(attrs, r"Mode:\s+0640")
        self.assertRegex(attrs, r"User:\s+123\s+Group:\s+456")
        inherited = self.debug("stat /lib/modules/modules.dep").stdout.decode()
        self.assertRegex(inherited, r"User:\s+0\s+Group:\s+2000")
        for name in ("sample.ko", "modules.dep"):
            target = self.root / "xattr"
            target.unlink(missing_ok=True)
            self.debug(f'ea_get -f "{target}" /lib/modules/{name} security.selinux')
            self.assertEqual(target.read_bytes(), self.context)
        self.command(["e2fsck", "-fn", self.image])

    def test_incomplete_module_coverage_is_rejected_before_replacement(self):
        original = self.read_file("/lib/modules/sample.ko")
        (self.replacements / "sample.ko").unlink()
        (self.replacements / "different.ko").write_bytes(b"wrong set")
        with self.assertRaises(ImageError):
            _vendor(self.image, self.replacements, self.root / "work")
        self.assertEqual(self.read_file("/lib/modules/sample.ko"), original)

    def test_missing_selinux_metadata_fails_closed(self):
        self.debug("ea_rm /lib/modules/sample.ko security.selinux", write=True)
        original = self.read_file("/lib/modules/sample.ko")
        with self.assertRaises(ImageError):
            _vendor(self.image, self.replacements, self.root / "work")
        self.assertEqual(self.read_file("/lib/modules/sample.ko"), original)

    def test_real_ext4_allocation_failure_is_not_reported_as_success(self):
        # debugfs may exit zero after an allocation failure. Nonzero data is
        # intentional: debugfs optimizes all-zero blocks into sparse holes.
        with (self.replacements / "sample.ko").open("wb") as stream:
            for _ in range(24):
                stream.write(b"x" * (1024 * 1024))
        with self.assertRaises(ImageError):
            _vendor(self.image, self.replacements, self.root / "work")


if __name__ == "__main__":
    unittest.main()
