"""Static checks for GitHub Actions build and ccache wiring."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "kernel.yml"


class GithubActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves GitHub's YAML 'on' mapping key.
        cls.data = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_build_runs_on_main_and_manual_dispatch(self):
        events = self.data["on"]
        self.assertIn("workflow_dispatch", events)
        self.assertEqual(events["push"]["branches"], ["main"])
        self.assertIn("pull_request", events)
        self.assertIn("github.event_name == 'push'", self.data["jobs"]["build"]["if"])
        self.assertEqual(self.data["jobs"]["build"]["strategy"]["matrix"]["device"],
                         "${{ fromJSON(needs.validate.outputs.devices) }}")
    def test_manual_dispatch_selects_one_or_both_devices(self):
        device_input = self.data["on"]["workflow_dispatch"]["inputs"]["device"]
        self.assertEqual(device_input["type"], "choice")
        self.assertEqual(device_input["default"], "both")
        self.assertEqual(device_input["options"], ["both", "lemonades", "capri"])
        validate = self.data["jobs"]["validate"]
        self.assertEqual(validate["outputs"]["devices"], "${{ steps.select.outputs.devices }}")
        select = next(s for s in validate["steps"] if s.get("id") == "select")
        self.assertEqual(select["env"]["REQUESTED_DEVICE"], "${{ inputs.device || 'both' }}")
        for choice, result in (
            ("both", 'devices=["lemonades","capri"]'),
            ("lemonades", 'devices=["lemonades"]'),
            ("capri", 'devices=["capri"]'),
        ):
            with self.subTest(choice=choice):
                self.assertIn(f'{choice}) echo \'{result}\'', select["run"])
        self.assertIn("github.run_id", self.data["concurrency"]["group"])
    def test_build_installs_and_persists_ccache(self):
        steps = self.data["jobs"]["build"]["steps"]
        install = next(s for s in steps if s.get("name") == "Install build dependencies and ccache")
        self.assertIn("ccache", install["run"])
        restore = next(s for s in steps if s.get("name") == "Restore ccache")
        self.assertEqual(restore["uses"], "actions/cache/restore@v4")
        self.assertIn("CCACHE_DIR", restore["with"]["path"])
        self.assertIn("github.run_id", restore["with"]["key"])
        self.assertIn("github.run_attempt", restore["with"]["key"])
        self.assertIn("restore-keys", restore["with"])
        save = next(s for s in steps if s.get("name") == "Save ccache immediately after compilation")
        self.assertEqual(save["uses"], "actions/cache/save@v4")
        self.assertIn("always()", save["if"])
        self.assertIn("!cancelled()", save["if"])
        self.assertEqual(save["with"]["key"], restore["with"]["key"])
        compile_index = next(i for i, s in enumerate(steps) if s.get("id") == "compile")
        save_index = steps.index(save)
        package_index = next(i for i, s in enumerate(steps) if s.get("name") == "Package compiled kernel")
        self.assertLess(compile_index, save_index)
        self.assertLess(save_index, package_index)
        self.assertEqual(self.data["jobs"]["build"]["env"]["CCACHE_COMPILERCHECK"], "content")
        llvm = next(s for s in steps if s.get("name") == "Restore pinned LLVM toolchain")
        self.assertEqual(llvm["uses"], "actions/cache@v4")
        self.assertIn("steps.llvm.outputs.identity", llvm["with"]["key"])
        self.assertIn(".work/_toolchains/kernel/llvm", llvm["with"]["path"])

    def test_uploads_kernel_artifacts_and_shows_stats(self):
        steps = self.data["jobs"]["build"]["steps"]
        build = next(s for s in steps if s.get("name") == "Compile selected kernel (DroidSpaces always enabled)")
        self.assertIn("python -m kernelforge build", build["run"])
        self.assertIn("matrix.device", build["run"])
        self.assertIn("-j \"$(nproc)\"", build["run"])
        self.assertNotIn("-j 4", build["run"])
        self.assertIn("--no-package", build["run"])
        package = next(s for s in steps if s.get("name") == "Package compiled kernel")
        self.assertIn("python -m kernelforge package", package["run"])
        self.assertIn("steps.compile.outcome == 'success'", package["if"])
        stats = next(s for s in steps if s.get("name") == "Report ccache performance")
        self.assertEqual(stats["if"], "always()")
        self.assertIn("GITHUB_STEP_SUMMARY", stats["run"])
        upload = next(s for s in steps if s.get("name") == "Upload generated AnyKernel3 ZIP and kernel images")
        self.assertEqual(upload["uses"], "actions/upload-artifact@v4")
        self.assertIn("matrix.device", upload["with"]["name"])
        for path in ("Image*", "*.zip", ".config"):
            self.assertIn(path, upload["with"]["path"])
        self.assertNotIn("dtb*", upload["with"]["path"])


if __name__ == "__main__":
    unittest.main()
