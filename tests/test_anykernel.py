"""YAML-driven AnyKernel3 packaging tests, no downloads or flashing."""
import copy
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import yaml

from kernelforge import cli


class AnyKernelTests(unittest.TestCase):
    def setUp(self):
        self.config = cli.load_config()

    def test_dynamic_device_and_boot_settings(self):
        cfg = copy.deepcopy(self.config["package"])
        cfg["device_check"] = True
        cfg["device"] = {
            "name": "Example Device",
            "codenames": ["example", "examplepro"],
            "android_versions": ["16", "17"],
        }
        cfg["boot"] = {
            "partition": "vendor_boot",
            "slots": "single",
            "ramdisk": "lz4",
            "vbmeta": "0",
            "install": "flash_boot",
        }
        cli.validate_package(cfg)
        script = cli.render_anykernel(cfg)
        for text in (
            "do.devicecheck=1",
            "device.name1=example\n",
            "device.name2=examplepro\n",
            "supported.versions=16, 17\n",
            "BLOCK=vendor_boot;",
            "IS_SLOT_DEVICE=0;",
            "RAMDISK_COMPRESSION=lz4;",
            "PATCH_VBMETA_FLAG=0;",
            "split_boot;\nflash_boot;\nflash_dtbo;",
        ):
            self.assertIn(text, script)
        self.assertNotIn("lemonades", script)
        subprocess.run(["sh", "-n"], input=script, text=True, check=True)

    def test_default_oneplus9r_script(self):
        script = cli.render_anykernel(self.config["package"])
        self.assertIn("do.devicecheck=0\n", script)
        self.assertIn("device.name1=lemonades\n", script)
        self.assertIn("supported.versions=17\n", script)
        self.assertIn("IS_SLOT_DEVICE=auto;", script)
        self.assertIn("BLOCK=boot;", script)
        self.assertIn("dump_boot;\nwrite_boot;", script)
        self.assertNotIn("maguro", script)
        subprocess.run(["sh", "-n"], input=script, text=True, check=True)

    def test_package_zip_contains_real_template_and_device_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            template = root / "AnyKernel3"
            core = template / "tools"
            core.mkdir(parents=True)
            (core / "ak3-core.sh").write_text("# sample pinned core\n", encoding="utf-8")
            binary = template / "META-INF/com/google/android/update-binary"
            binary.parent.mkdir(parents=True)
            binary.write_text("# sample update-binary\n", encoding="utf-8")
            (template / "README.md").write_text("sample readme\n", encoding="utf-8")
            (template / "anykernel.sh").write_text("bad template; do.devicecheck=0\n", encoding="utf-8")
            (template / ".git").mkdir()
            (template / ".git/config").write_text("ignore\n", encoding="utf-8")
            (template / "ramdisk").mkdir()
            (template / "ramdisk/placeholder").write_text("ignore\n", encoding="utf-8")
            output = work / cli.WORK_NAME / "out"
            output.mkdir(parents=True, exist_ok=True)
            (work / cli.WORK_NAME / ".kernelforge-managed").write_text(
                "test workspace\n", encoding="utf-8")
            for path in self.config["package"]["images"]:
                target = output / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"KERNEL-"+path.encode("utf-8"))

            with patch.object(cli, "WORK", work), \
                 patch.object(cli, "bootstrap_anykernel", return_value=template), \
                 redirect_stdout(io.StringIO()):
                result = cli.package(cli.WORK_NAME, self.config)

            import zipfile
            self.assertTrue(result.is_file())
            with zipfile.ZipFile(result) as zf:
                paths = set(zf.namelist())
                self.assertIn("anykernel.sh", paths)
                self.assertIn("Image", paths)
                self.assertIn("dtb", paths)
                self.assertIn("dtbo.img", paths)
                self.assertIn("META-INF/com/google/android/update-binary", paths)
                self.assertIn("tools/ak3-core.sh", paths)
                self.assertNotIn(".git/config", paths)
                self.assertNotIn("README.md", paths)
                self.assertNotIn("ramdisk/placeholder", paths)
                script = zf.read("anykernel.sh").decode()
                self.assertIn("device.name1=lemonades\n", script)
                self.assertIn("do.devicecheck=0\n", script)
                self.assertNotIn("maguro", script)
                self.assertEqual(zf.read("Image"), b"KERNEL-arch/arm64/boot/Image")

    def test_rejects_unsafe_device_settings_and_overwriting_installer(self):
        cases = [
            ("name", lambda x: x["device"].update({"name": "bad' ; reboot"})),
            ("codename", lambda x: x["device"].update({"codenames": ["lemonades; reboot"]})),
            ("missing_code", lambda x: x["device"].update({"codenames": []})),
            ("android_bool", lambda x: x["device"].update({"android_versions": [17]})),
            ("unsafe_output", lambda x: x.update({"output": "../../kernel.zip"})),
            ("missing_image", lambda x: x.update({"images": {"arch/arm64/boot/dtb": "dtb"}})),
            ("installer_overwrite", lambda x: x.update({"images": {"arch/arm64/boot/Image": "anykernel.sh"}})),
            ("meta_overwrite", lambda x: x.update({"images": {"arch/arm64/boot/Image": "META-INF/x"}})),
            ("unknown_slot", lambda x: x["boot"].update({"slots": "thirdslot"})),
            ("unsafe_partition", lambda x: x["boot"].update({"partition": "../boot"})),
            ("missing_pin", lambda x: x["anykernel"].pop("commit")),
            ("invalid_device_check", lambda x: x.update({"device_check": "false"})),
        ]
        for name, modify in cases:
            with self.subTest(case=name):
                cfg = copy.deepcopy(self.config["package"])
                modify(cfg)
                with self.assertRaises(cli.ForgeError):
                    cli.validate_package(cfg)

    def test_build_cli_always_packages_even_in_dry_run(self):
        with patch.object(cli, "load_config", return_value=self.config), \
             patch.object(cli, "build"), \
             patch.object(cli, "package") as package:
            code = cli.main(["build", "--dry-run", "-j", "3"])
        self.assertEqual(code, 0)
        package.assert_called_once_with(cli.WORK_NAME, self.config, dry=True)

    def test_absent_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = dict(self.config)
            copy.pop("package")
            p = Path(directory) / "kernel.yaml"
            p.write_text(yaml.safe_dump(copy), encoding="utf-8")
            with patch.object(cli, "CONFIG", p):
                with self.assertRaises(cli.ForgeError):
                    cli.load_config()


if __name__ == "__main__":
    unittest.main()
