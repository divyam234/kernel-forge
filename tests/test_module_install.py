"""Filesystem behavior of sourced installer helpers; no devices or phone access."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "assets/install-modules.sh"


class ModuleInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "payload"
        self.target = self.root / "vendor"
        self.backup = self.root / "backup"
        self.source.mkdir()
        self.target.mkdir()
        for name in ("one.ko", "two.ko"):
            (self.source / name).write_bytes(b"new-" + name.encode())
            (self.target / name).write_bytes(b"old-" + name.encode())
            (self.target / name).chmod(0o640)
        for name in ("modules.dep", "modules.alias", "modules.softdep"):
            (self.source / name).write_text("replacement metadata\n")
        (self.target / "modules.dep").write_text("original metadata\n")
        (self.target / "modules.load").write_text("two\none\n")
        shutil.copytree(self.target, self.backup, copy_function=shutil.copy2)
        self.manifest = self.source / "modules.sha256"
        self.manifest.write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(self.source.iterdir())
        ))

    def shell(self, script, *args):
        # Label operations cannot be exercised on ordinary host tmpfs/ext4. These
        # shims isolate file replacement tests, not production mount/flash paths.
        setup = '''
. "$1"
shift
chcon() { return 0; }
chown() { shift; command chown "$(id -u):$(id -g)" "$@"; }
'''
        return subprocess.run(
            ["sh", "-c", setup + script, "test", str(INSTALLER), *map(str, args)],
            text=True, capture_output=True,
        )

    def test_checked_replacement_preserves_rom_load_and_rollback_attributes(self):
        result = self.shell('_kf_copy_files "$1" "$2" "$3" "$4"',
                            self.source, self.target, self.backup, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "one.ko").read_bytes(), b"new-one.ko")
        self.assertEqual((self.target / "one.ko").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.target / "modules.load").read_text(), "two\none\n")
        result = self.shell('_kf_restore_files "$1" "$2" "$3"',
                            self.target, self.backup, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "one.ko").read_bytes(), b"old-one.ko")
        self.assertEqual((self.target / "one.ko").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.target / "modules.dep").read_text(), "original metadata\n")
        self.assertFalse((self.target / "modules.alias").exists())
        self.assertFalse((self.target / "modules.softdep").exists())
        self.assertEqual((self.target / "modules.load").read_text(), "two\none\n")

    def test_partial_copy_failure_can_restore_original_files(self):
        # First manifest entry succeeds; second cp fails against a real missing input.
        self.manifest.write_text(
            f"{hashlib.sha256((self.source / 'one.ko').read_bytes()).hexdigest()}  one.ko\n"
            + "0" * 64 + "  missing.ko\n")
        result = self.shell('''
_kf_copy_files "$1" "$2" "$3" "$4"
status=$?
[ "$status" -ne 0 ] || exit 99
_kf_restore_files "$2" "$3" "$4"
''', self.source, self.target, self.backup, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "one.ko").read_bytes(), b"old-one.ko")
        self.assertFalse((self.target / "missing.ko").exists())

    def test_coordinated_restore_recovers_both_images_and_module_set(self):
        rollback = self.root / "rollback"
        rollback.mkdir()
        shutil.copytree(self.backup, rollback / "modules", copy_function=shutil.copy2)
        (rollback / "labels").write_text("")
        (rollback / "original.sha256").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(self.backup.iterdir()) if p.name != "modules.load"
        ))
        boot = self.root / "boot-device"
        vendor_boot = self.root / "vendor-boot-device"
        for filename, target in (("boot.img", boot), ("vendor_boot.img", vendor_boot)):
            (rollback / filename).write_bytes((b"old-" + filename.encode()).ljust(4096, b"\0"))
            target.write_bytes(b"incomplete new image".ljust(4096, b"\0"))
        (self.target / "one.ko").write_bytes(b"incompatible-new-module")
        result = self.shell('_kf_restore_install "$1" "$2" "$3" "$4" "$5"',
                            rollback, boot, vendor_boot, self.target, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(boot.read_bytes(), (rollback / "boot.img").read_bytes())
        self.assertEqual(vendor_boot.read_bytes(), (rollback / "vendor_boot.img").read_bytes())
        self.assertEqual((self.target / "one.ko").read_bytes(), b"old-one.ko")
        self.assertEqual((self.target / "one.ko").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.target / "modules.load").read_text(), "two\none\n")
        # A failed partition restore must not be reported as a coherent success.
        (rollback / "boot.img").unlink()
        result = self.shell('_kf_restore_install "$1" "$2" "$3" "$4" "$5"',
                            rollback, boot, vendor_boot, self.target, self.manifest)
        self.assertNotEqual(result.returncode, 0)

    def test_checksum_corruption_and_path_traversal_are_rejected(self):
        result = self.shell('_kf_verify "$1" "$2"', self.source, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.source / "one.ko").write_bytes(b"corrupted")
        result = self.shell('_kf_verify "$1" "$2"', self.source, self.manifest)
        self.assertNotEqual(result.returncode, 0)
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        self.manifest.write_text(f"{hashlib.sha256(outside.read_bytes()).hexdigest()}  ../outside\n")
        result = self.shell('_kf_verify "$1" "$2"', self.source, self.manifest)
        self.assertNotEqual(result.returncode, 0)

    def test_module_inventory_rejects_missing_extra_and_symlink_inputs(self):
        inventory = self.root / "inventory"
        result = self.shell('_kf_same_modules "$1" "$2" "$3"',
                            self.source, self.target, inventory)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.target / "two.ko").unlink()
        result = self.shell('_kf_same_modules "$1" "$2" "$3"',
                            self.source, self.target, inventory)
        self.assertNotEqual(result.returncode, 0)
        os.symlink(self.source / "two.ko", self.target / "two.ko")
        result = self.shell('_kf_same_modules "$1" "$2" "$3"',
                            self.source, self.target, inventory)
        self.assertNotEqual(result.returncode, 0)

    def test_vendor_slot_identity_rejects_the_other_slot(self):
        active = self.root / "vendor-b-device"
        inactive = self.root / "vendor-a-device"
        active.write_bytes(b"slot-b")
        inactive.write_bytes(b"slot-a")
        alias = self.root / "vendor_b"
        alias.symlink_to(active)
        cases = [
            (active, alias, "vendor_b", "_b", True),
            (inactive, alias, "vendor_a", "_b", False),
            (inactive, alias, "vendor_b", "_b", False),
            (active, "", "vendor_b", "_b", True),
            (inactive, "", "vendor_a", "_b", False),
            (active, "", "", "_b", False),
            (active, alias, "", "_b", True),
        ]
        for source, expected, mapping, slot, accepted in cases:
            with self.subTest(source=source, mapping=mapping):
                result = self.shell('_kf_vendor_slot_matches "$1" "$2" "$3" "$4"',
                                    source, expected, mapping, slot)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_only_complete_linear_device_mapper_tables_are_accepted(self):
        cases = [
            ("0 1089176 linear 259:43 18445712\n", True),
            ("Targets in the device-mapper table for vendor_b:\n"
             "0-1089176: linear, 259:43 18445712\n", True),
            ("0 100 linear 259:43 0\n100 100 verity 1 /dev/dm-1\n", False),
            ("0-100: linear, 259:43 0\n100-200: crypt, 259:44 0\n", False),
            ("0 100 not-linear 259:43 0\n", False),
            ("0 100 linear 259:43 not-a-number\n", False),
            ("0:1 100 linear 259:43 0\n", False),
            ("0 100 linear 259:43:44 0\n", False),
            ("0 100 linear 259:43 0 ignored\n", False),
            ("", False),
        ]
        for table, accepted in cases:
            with self.subTest(table=table):
                result = self.shell('printf "%s" "$1" | _kf_linear_table vendor_b', table)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)


if __name__ == "__main__":
    unittest.main()
