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
        fragment = cli.local_path(data["droidspaces"]["config"], "droidspaces.config")
        content = fragment.read_text(encoding="utf-8")
        for option in ("CONFIG_NET_NS=y", "CONFIG_MEMCG=y", "CONFIG_OVERLAY_FS=y"):
            self.assertIn(option, content)
        self.assertNotIn("strict_config", data["build"])

    def test_build_does_not_verify_kconfig_symbols(self):
        data = cli.load_config("capri")
        self.assertNotIn("strict_config", data["build"])
        self.assertFalse(hasattr(cli, "verify_config"))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cli, "WORK", Path(directory) / ".work"), redirect_stdout(io.StringIO()) as output:
                cli.build(cli.workspace_name("capri"), data, 2, dry=True)
        preview = output.getvalue()
        self.assertIn("vendor/droidspaces.config", preview)
        self.assertIn("-j2", preview)
        self.assertNotIn("VERIFY generated .config", preview)


    def test_systemd_backports_present_and_enabled(self):
        data = cli.load_config()
        backports = data["patches"][:4]
        self.assertEqual(len(backports), 4)
        self.assertTrue(backports[0]["file"].endswith("0003-statx-add-mount-ID.patch"))
        self.assertTrue(backports[1]["file"].endswith("0004-statx-add-mount_root.patch"))
        self.assertTrue(backports[2]["file"].endswith("0005-utimensat-AT_EMPTY_PATH-support.patch"))
        self.assertTrue(backports[3]["file"].endswith("0006-faccessat2-lineage24.patch"))
        self.assertEqual([p.get("enabled", True) for p in backports],
                         [True, True, True, True])
        self.assertTrue(data["patches"][4]["file"].endswith("0001-lxc-cgroup-prefix.patch"))
        self.assertTrue(data["patches"][4]["enabled"])
        for p in data["patches"]:
            self.assertTrue(cli.local_path(p["file"], "patch").is_file(), p["file"])

    def test_patch_files_end_with_newline(self):
        data = cli.load_config()
        for entry in data["patches"]:
            path = cli.local_path(entry["file"], "patch")
            with self.subTest(patch=entry["file"]):
                self.assertTrue(path.read_bytes().endswith(b"\n"),
                                f"{entry['file']} must end with a newline")

    def test_dry_run_does_not_touch_workspace(self):
        data = cli.load_config()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cli, "WORK", Path(tmp) / "workspace"):
                out = io.StringIO()
                with redirect_stdout(out):
                    cli.prepare(cli.WORK_NAME, data, dry=True)
                preview = out.getvalue()
                self.assertIn(" apply --check ", preview)
                self.assertIn("COPY", preview)
                self.assertLess(preview.index("0002-bakasu-manual-hooks-4.19.patch"),
                                preview.index("0003-statx-add-mount-ID.patch"))
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

    def test_anykernel_package_always_configured(self):
        data = cli.load_config()
        self.assertEqual(data["package"]["type"], "anykernel3")
        self.assertEqual(data["package"]["device"]["codenames"], ["lemonades"])
        self.assertEqual(data["package"]["boot"]["slots"], "auto")
        with patch.object(cli, "WORK", Path("/tmp/kernelforge-tests")):
            with redirect_stdout(io.StringIO()):
                artifact = cli.package(cli.WORK_NAME, data, dry=True)
        self.assertEqual(artifact.name, "DroidSpaces-lemonades.zip")


if __name__ == "__main__":
    unittest.main()
