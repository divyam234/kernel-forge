"""Consumer-level module staging tests using genuine synthetic ARM64 ELF files."""

import hashlib
from pathlib import Path
import shutil
import struct
import tempfile
import tarfile
import unittest

from elftools.elf.elffile import ELFFile

from kernelforge.modules import ModuleError, archive_modules, stage_modules


RELEASE = "4.19.325-capri"
CRCS = {"module_layout": 0x5729D323, "kernel_api": 0x12345678}


def module_elf(name, *, depends=(), aliases=(), softdeps=(), release=RELEASE,
               versions=None, required=("kernel_api",), weak=()):
    """Construct ELF64 sections, including real undefined symbol/version tables."""
    versions = dict(CRCS if versions is None else versions)
    info = [f"name={name}", f"vermagic={release} SMP preempt mod_unload modversions aarch64",
            f"depends={','.join(depends)}"]
    info += [f"alias={a}" for a in aliases] + [f"softdep={s}" for s in softdeps]
    strings = bytearray(b"\0")
    symbols = bytearray(24)  # ELF's mandatory null symbol
    for symbol, bind in [(s, 1) for s in required] + [(s, 2) for s in weak]:
        offset = len(strings)
        strings += symbol.encode() + b"\0"
        symbols += struct.pack("<IBBHQQ", offset, bind << 4, 0, 0, 0, 0)
    version_data = b"".join(struct.pack("<Q56s", crc, symbol.encode())
                            for symbol, crc in versions.items())
    sections = [(".modinfo", 1, "\0".join(info).encode() + b"\0", 0, 0, 1, 0),
                ("__versions", 1, version_data, 0, 0, 8, 0),
                (".strtab", 3, bytes(strings), 0, 0, 1, 0),
                (".symtab", 2, bytes(symbols), 3, 1, 8, 24),
                (".debug_info", 1, b"synthetic debug information", 0, 0, 1, 0)]
    shstrings = b"\0" + b"".join(s[0].encode() + b"\0" for s in sections) + b".shstrtab\0"
    sections.append((".shstrtab", 3, shstrings, 0, 0, 1, 0))
    data = bytearray(64)
    headers = [bytes(64)]
    for name, kind, content, link, info, align, entsize in sections:
        data += bytes((-len(data)) % align)
        offset = len(data)
        data += content
        headers.append(struct.pack("<IIQQQQIIQQ", shstrings.index(name.encode() + b"\0"),
                                   kind, 0, 0, offset, len(content), link, info, align, entsize))
    data += bytes((-len(data)) % 8)
    shoff = len(data)
    data += b"".join(headers)
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    data[:64] = struct.pack("<16sHHIQQQIHHHHHH", ident, 1, 183, 1, 0, 0, shoff,
                            0, 64, 0, 0, 64, len(headers), len(sections))
    return bytes(data)


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.stage = self.root / "stage"
        (self.out / "include/config").mkdir(parents=True)
        (self.out / "include/config/kernel.release").write_text(RELEASE + "\n")
        (self.out / "Module.symvers").write_text("".join(
            f"0x{crc:08x}\t{symbol}\tvmlinux\tEXPORT_SYMBOL\n" for symbol, crc in CRCS.items()))
        self.order = []
        (self.out / "modules.order").write_text("")

    def add_module(self, filename, name=None, **kwargs):
        path = self.out / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        content = module_elf(name or path.stem, **kwargs)
        path.write_bytes(content)
        self.order.append(filename)
        (self.out / "modules.order").write_text("\n".join(self.order) + "\n")
        return content

    @property
    def vendor(self):
        return self.stage / "module-files/vendor/lib/modules"

    @property
    def early(self):
        return self.stage / "vendor_boot-files/ramdisk/lib/modules"

    def test_aliases_and_transitive_dependency_order(self):
        original = self.add_module("drivers/top.ko", name="top_internal", depends=("left-side", "right"),
                                   aliases=("platform:capri*",), softdeps=("pre: left-side post: right",))
        self.add_module("left.ko", name="left_side", depends=("bottom",))
        self.add_module("right.ko", depends=("bottom",))
        self.add_module("bottom.ko")
        cfg = {"aliases": {"top.ko": "renamed.ko", "bottom.ko": "base.ko", "left.ko": "left-file.ko"},
               "vendor_boot": []}
        stage_modules(self.out, self.stage, cfg)
        self.assertIn("renamed.ko: left-file.ko right.ko base.ko\n", (self.vendor / "modules.dep").read_text())
        self.assertEqual((self.vendor / "modules.alias").read_text(), "alias platform:capri* renamed\n")
        self.assertEqual((self.vendor / "modules.softdep").read_text(),
                         "softdep renamed pre: left_file post: right\n")
        self.assertFalse((self.vendor / "modules.load").exists())
        self.assertEqual((self.vendor / "renamed.ko").read_bytes(), original)
        self.assertEqual((self.out / "drivers/top.ko").read_bytes(), original)
        for directory, manifest in [(self.vendor, self.vendor / "modules.sha256"),
                                    (self.early, self.stage / "module-files/early.sha256")]:
            lines = manifest.read_text().splitlines()
            self.assertEqual([line.split("  ")[1] for line in lines],
                             sorted(p.name for p in directory.iterdir() if p != manifest))
            for line in lines:
                digest, filename = line.split("  ")
                self.assertEqual(digest, hashlib.sha256((directory / filename).read_bytes()).hexdigest())

    def test_linux419_install_prefix_stages_and_archives_build_paths(self):
        original = self.add_module("drivers/example.ko")
        (self.out / "modules.order").write_text("kernel/drivers/example.ko\n")
        stage_modules(self.out, self.stage, {"vendor_boot": ["example.ko"]})
        self.assertEqual((self.vendor / "example.ko").read_bytes(), original)
        archive = self.root / "modules.tar.gz"
        archive_modules(self.out, archive)
        with tarfile.open(archive) as bundle:
            self.assertEqual(bundle.extractfile("drivers/example.ko").read(), original)
            self.assertEqual(bundle.extractfile("modules.order").read(),
                             b"kernel/drivers/example.ko\n")
            self.assertEqual(bundle.extractfile("include/config/kernel.release").read(),
                             (RELEASE + "\n").encode())

    def test_internal_module_load_names_remain_resolvable(self):
        self.add_module("provider.ko")
        self.add_module("consumer.ko", depends=("provider",))
        stage_modules(self.out, self.stage, {"vendor_boot": ["provider.ko", "consumer.ko"]})
        self.assertEqual((self.early / "modules.load").read_text(), "provider\nconsumer\n")
        self.assertEqual((self.early / "modules.dep").read_text(),
                         "provider.ko:\nconsumer.ko: provider.ko\n")

    def test_renamed_early_module_is_rejected(self):
        self.add_module("touch.ko")
        with self.assertRaisesRegex(ModuleError, "installed filename must match"):
            stage_modules(self.out, self.stage, {
                "aliases": {"touch.ko": "renamed.ko"}, "vendor_boot": ["touch.ko"]})

    def test_ambiguous_install_prefix_cannot_select_stale_module(self):
        self.add_module("drivers/example.ko", name="example")
        other = self.out / "kernel/drivers/example.ko"
        other.parent.mkdir(parents=True)
        other.write_bytes(module_elf("other"))
        (self.out / "modules.order").write_text("kernel/drivers/example.ko\n")
        with self.assertRaisesRegex(ModuleError, "ambiguous build path"):
            stage_modules(self.out, self.stage, {})

    def test_wrong_crc_rejected(self):
        self.add_module("wrong.ko", versions={**CRCS, "kernel_api": 0xBAD})
        with self.assertRaisesRegex(ModuleError, "CRC mismatch for kernel_api"):
            stage_modules(self.out, self.stage, {})

    def test_wrong_module_layout_rejected(self):
        self.add_module("wrong.ko", versions={**CRCS, "module_layout": 0x7B99C8EF})
        with self.assertRaisesRegex(ModuleError, "CRC mismatch for module_layout"):
            stage_modules(self.out, self.stage, {})

    def test_wrong_release_rejected(self):
        self.add_module("wrong.ko", release="4.19.325-other")
        with self.assertRaisesRegex(ModuleError, "vermagic release"):
            stage_modules(self.out, self.stage, {})

    def test_missing_strong_crc_rejected(self):
        self.add_module("wrong.ko", required=("kernel_api", "missing_import"))
        with self.assertRaisesRegex(ModuleError, "missing imported CRCs: missing_import"):
            stage_modules(self.out, self.stage, {})

    def test_optional_weak_import_without_crc_is_valid(self):
        self.add_module("valid.ko", weak=("optional_hook",))
        stage_modules(self.out, self.stage, {})
        self.assertTrue((self.vendor / "valid.ko").is_file())

    def test_missing_module_layout_rejected(self):
        self.add_module("wrong.ko", versions={"kernel_api": CRCS["kernel_api"]})
        with self.assertRaisesRegex(ModuleError, "missing imported CRCs: module_layout"):
            stage_modules(self.out, self.stage, {})

    def test_early_dependency_outside_subset_rejected(self):
        self.add_module("consumer.ko", depends=("provider",))
        self.add_module("provider.ko")
        with self.assertRaisesRegex(ModuleError, "early dependency outside configured subset: provider.ko"):
            stage_modules(self.out, self.stage, {"vendor_boot": ["consumer.ko"]})

    def test_missing_dependency_rejected(self):
        self.add_module("consumer.ko", depends=("absent",))
        with self.assertRaisesRegex(ModuleError, "dependency 'absent' is not in modules.order"):
            stage_modules(self.out, self.stage, {})

    def test_missing_module_rejected(self):
        (self.out / "modules.order").write_text("drivers/missing.ko\n")
        with self.assertRaisesRegex(ModuleError, "missing module"):
            stage_modules(self.out, self.stage, {})

    def test_object_paths_resolve_to_modules(self):
        self.add_module("drivers/module.ko")
        (self.out / "modules.order").write_text("drivers/module.o\n")
        stage_modules(self.out, self.stage, {})
        self.assertTrue((self.vendor / "module.ko").is_file())

    def test_path_escape_rejected(self):
        (self.out / "modules.order").write_text("../outside.ko\n")
        with self.assertRaisesRegex(ModuleError, "unsafe"):
            stage_modules(self.out, self.stage, {})

    def test_symlink_escape_rejected(self):
        outside = self.root / "outside.ko"
        outside.write_bytes(module_elf("outside"))
        (self.out / "outside.ko").symlink_to(outside)
        (self.out / "modules.order").write_text("outside.ko\n")
        with self.assertRaisesRegex(ModuleError, "escaping path"):
            stage_modules(self.out, self.stage, {})

    def test_duplicate_basename_rejected(self):
        self.add_module("one/same.ko", name="one")
        self.add_module("two/same.ko", name="two")
        with self.assertRaisesRegex(ModuleError, "duplicate module basename"):
            stage_modules(self.out, self.stage, {})

    def test_normalized_destination_collision_rejected(self):
        self.add_module("one.ko")
        self.add_module("two.ko")
        with self.assertRaisesRegex(ModuleError, "destination collisions"):
            stage_modules(self.out, self.stage, {"aliases": {"one.ko": "a-b.ko", "two.ko": "a_b.ko"}})

    def test_unknown_alias_source_rejected(self):
        self.add_module("one.ko")
        with self.assertRaisesRegex(ModuleError, "aliases must map built module"):
            stage_modules(self.out, self.stage, {"aliases": {"absent.ko": "new.ko"}})

    def test_dependency_cycle_rejected(self):
        self.add_module("one.ko", depends=("two",))
        self.add_module("two.ko", depends=("one",))
        with self.assertRaisesRegex(ModuleError, "dependency cycle"):
            stage_modules(self.out, self.stage, {})

    def test_real_llvm_strip_preserves_source_and_loading_metadata(self):
        tool = shutil.which("llvm-strip")
        if tool is None:
            self.skipTest("llvm-strip is not installed")
        original = self.add_module("debug.ko")
        stage_modules(self.out, self.stage, {}, strip_tool=Path(tool))
        self.assertEqual((self.out / "debug.ko").read_bytes(), original)
        with (self.vendor / "debug.ko").open("rb") as stream:
            self.assertIsNone(ELFFile(stream).get_section_by_name(".debug_info"))
        self.assertEqual((self.vendor / "modules.dep").read_text(), "debug.ko:\n")

    def test_signed_module_cannot_be_stripped(self):
        original = self.add_module("signed.ko") + b"~Module signature appended~\n"
        (self.out / "signed.ko").write_bytes(original)
        with self.assertRaisesRegex(ModuleError, "cannot strip a signed module"):
            stage_modules(self.out, self.stage, {}, strip_tool=Path("unused-llvm-strip"))
        self.assertEqual((self.out / "signed.ko").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
