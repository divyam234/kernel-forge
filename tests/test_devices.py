"""Device selection and shared DroidSpaces configuration."""
import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import jsonschema
import yaml

from kernelforge import cli


class DeviceTests(unittest.TestCase):
    def test_capri_uses_official_lineage24_kernel_config(self):
        capri = cli.load_config("capri")
        self.assertEqual(capri["source"]["url"],
                         "https://github.com/LineageOS/android_kernel_motorola_sm6225.git")
        self.assertEqual(capri["source"]["ref"], "lineage-24.0")
        self.assertEqual(capri["source"]["commit"],
                         "1da2d0861244d324cfff0fcb31dec374dd94436b")
        self.assertEqual(capri["build"]["defconfig"], "vendor/bengal-perf_defconfig")
        self.assertEqual(capri["build"]["fragments"], [
            "vendor/ext_config/moto-bengal.config",
            "vendor/ext_config/capri-default.config",
        ])
        self.assertEqual(capri["build"]["artifacts"], ["arch/arm64/boot/Image"])
        self.assertEqual(capri["package"]["images"], {"arch/arm64/boot/Image": "Image"})
        self.assertEqual(capri["package"]["device"]["codenames"], ["capri"])
        self.assertEqual(capri["package"]["device"]["android_versions"], ["17"])
        self.assertEqual(capri["package"]["output"], "DroidSpaces-capri.zip")
        self.assertEqual(cli.workspace_name("capri"), "kernel-capri")

    def test_both_devices_share_droidspaces_and_cgroup_patch(self):
        oneplus = cli.load_config()
        motorola = cli.load_config("capri")
        self.assertEqual(oneplus["droidspaces"], motorola["droidspaces"])
        self.assertEqual(oneplus["patches"], motorola["patches"])
        self.assertEqual(oneplus["toolchain"], motorola["toolchain"])
        enabled = [p["file"] for p in motorola["patches"] if p.get("enabled", True)]
        self.assertEqual(enabled, ["patches/0001-lxc-cgroup-prefix.patch"])
        self.assertEqual(motorola["package"]["device_check"], False)
        self.assertEqual(motorola["package"]["anykernel"], oneplus["package"]["anykernel"])
        self.assertEqual(motorola["build"]["ccache"], oneplus["build"]["ccache"])
        self.assertEqual(oneplus["package"]["device"]["codenames"], ["lemonades"])
        self.assertNotEqual(motorola["source"], oneplus["source"])

    def test_capri_anykernel_and_build_preview(self):
        data = cli.load_config("capri")
        script = cli.render_anykernel(data["package"])
        self.assertIn("device.name1=capri\n", script)
        self.assertIn("do.devicecheck=0\n", script)
        self.assertIn("BLOCK=boot;", script)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(cli, "WORK", Path(temp) / ".work"), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["build", "--device", "capri", "--dry-run"]), 0)
        log = output.getvalue()
        self.assertIn("android_kernel_motorola_sm6225", log)
        self.assertIn("vendor/bengal-perf_defconfig", log)
        self.assertIn("vendor/ext_config/capri-default.config", log)
        self.assertIn("assets/droidspaces.config", log)
        self.assertIn("0001-lxc-cgroup-prefix.patch", log)
        self.assertNotIn("git apply --check " + str(cli.ROOT / "patches/backports-4.19"), log)
        self.assertIn("DroidSpaces-capri.zip", log)

    def test_reject_unknown_device_and_path_traversal(self):
        for value in ("not-a-device", "../capri", "/tmp/invalid", "capri/../../"):
            with self.subTest(value=value), self.assertRaises(cli.ForgeError):
                cli.load_config(value)

    def test_device_cannot_override_shared_droidspaces_or_patches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "capri.yaml"
            path.write_text("droidspaces:\n  config: anything\n", encoding="utf-8")
            with patch.object(cli, "DEVICES", Path(temp)):
                with self.assertRaisesRegex(cli.ForgeError, "unknown keys"):
                    cli.load_config("capri")


    def test_shared_device_and_merged_schemas(self):
        common = yaml.safe_load(cli.CONFIG.read_text(encoding="utf-8"))
        common_schema = json.loads((cli.ROOT / "schema/common.schema.json").read_text())
        device_schema = json.loads((cli.ROOT / "schema/device.schema.json").read_text())
        merged_schema = json.loads((cli.ROOT / "schema/kernel.schema.json").read_text())
        jsonschema.validate(common, common_schema)
        for name in ("lemonades", "capri"):
            with self.subTest(device=name):
                device = yaml.safe_load((cli.DEVICES / (name + ".yaml")).read_text())
                jsonschema.validate(device, device_schema)
                jsonschema.validate(cli.load_config(name), merged_schema)


if __name__ == "__main__":
    unittest.main()
