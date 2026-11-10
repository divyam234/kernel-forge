import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import patch

from kernelforge import cli


class KernelTests(unittest.TestCase):
    def test_one_config_droidspaces_mandatory(self):
        data = cli.load_config()
        self.assertEqual(data["source"]["ref"], "lineage-24.0")
        self.assertEqual(data["droidspaces"]["target"], "vendor/droidspaces.config")
        self.assertEqual(cli.requested_settings(data)["CONFIG_NET_NS"], "y")
        self.assertEqual(cli.requested_settings(data)["CONFIG_MEMCG"], "y")
        self.assertEqual(cli.requested_settings(data)["CONFIG_OVERLAY_FS"], "y")

    def test_backports_present_and_ordered(self):
        data = cli.load_config()
        enabled = [p["file"] for p in data["patches"] if p.get("enabled", True)]
        self.assertEqual(len(enabled), 5)
        self.assertTrue(enabled[0].endswith("0003-statx-add-mount-ID.patch"))
        self.assertTrue(enabled[1].endswith("0004-statx-add-mount_root.patch"))
        self.assertTrue(enabled[2].endswith("0005-utimensat-AT_EMPTY_PATH-support.patch"))
        self.assertTrue(enabled[3].endswith("0006-faccessat2-lineage24.patch"))
        self.assertTrue(enabled[4].endswith("0001-lxc-cgroup-prefix.patch"))
        for p in enabled:
            self.assertTrue(cli.local_path(p, "patch").is_file(), p)

    def test_dry_run_does_not_touch_workspace(self):
        data = cli.load_config()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cli, "WORK", Path(tmp) / "workspace"):
                out = io.StringIO()
                with redirect_stdout(out):
                    cli.prepare(cli.WORK_NAME, data, dry=True)
                self.assertIn(" apply --check ", out.getvalue())
                self.assertIn("COPY", out.getvalue())
                self.assertFalse((Path(tmp) / "workspace").exists())

    def test_reject_unmarked_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "kernel").mkdir()
            with patch.object(cli, "WORK", Path(tmp)):
                with self.assertRaises(cli.ForgeError):
                    cli.managed("kernel")

    def test_config_must_include_droidspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kernel.yaml"
            p.write_text("apiVersion: kernelforge/v1\nsource: {}\n", encoding="utf-8")
            with patch.object(cli, "CONFIG", p):
                with self.assertRaises(cli.ForgeError):
                    cli.load_config()

    def test_unknown_config_keys_rejected(self):
        data = cli.load_config()
        data["profile"] = "not allowed"
        with self.assertRaises(cli.ForgeError):
            cli.fields(data, cli.SUPPORTED, "kernel.yaml")

    def test_path_traversal_rejected(self):
        for item in ("/tmp/file", "../private", "abc/../oops", ".git/index"):
            with self.subTest(item=item):
                with self.assertRaises(cli.ForgeError):
                    cli.local_path(item, "test")

    def test_package_disabled_until_device_template_exists(self):
        data = cli.load_config()
        with self.assertRaises(cli.ForgeError):
            cli.package(cli.WORK_NAME, data, dry=True)


if __name__ == "__main__":
    unittest.main()
