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

    def test_build_runs_only_on_manual_dispatch(self):
        events = self.data["on"]
        self.assertEqual(set(events), {"workflow_dispatch"})
        self.assertNotIn("if", self.data["jobs"]["build"])
        self.assertEqual(self.data["concurrency"]["cancel-in-progress"], "false")
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
        self.assertEqual(restore["uses"], "actions/cache/restore@v6")
        self.assertIn("CCACHE_DIR", restore["with"]["path"])
        self.assertIn("github.run_id", restore["with"]["key"])
        self.assertIn("github.run_attempt", restore["with"]["key"])
        self.assertIn("restore-keys", restore["with"])
        save = next(s for s in steps if s.get("name") == "Save ccache immediately after compilation")
        self.assertEqual(save["uses"], "actions/cache/save@v6")
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
        self.assertEqual(llvm["uses"], "actions/cache@v6")
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
        stage = next(s for s in steps if s.get("name") == "Stage compiled kernel outputs")
        self.assertIn("workspace_name", stage["run"])
        self.assertIn('cp "$output/arch/arm64/boot/Image"', stage["run"])
        self.assertIn('cp "$output/.config"', stage["run"])
        self.assertIn("dist/compiled/$device/kernel.config", stage["run"])
        self.assertIn("always()", stage["if"])
        self.assertIn("!cancelled()", stage["if"])
        compiled = next(s for s in steps if s.get("name") == "Upload compiled kernel before packaging")
        self.assertEqual(compiled["uses"], "actions/upload-artifact@v7")
        self.assertIn("dist/compiled/", compiled["with"]["path"])
        self.assertNotIn(".work/", compiled["with"]["path"])
        self.assertIn("always()", compiled["if"])
        self.assertIn("!cancelled()", compiled["if"])
        package_stage = next(s for s in steps if s.get("id") == "package")
        self.assertIn("dist/packages/$device/", package_stage["run"])
        self.assertIn("always()", package_stage["if"])
        zip_upload = next(s for s in steps if s.get("name") == "Upload packaged AnyKernel3 ZIP")
        self.assertEqual(zip_upload["uses"], "actions/upload-artifact@v7")
        self.assertIn("dist/packages/", zip_upload["with"]["path"])
        self.assertIn("DroidSpaces-", zip_upload["with"]["path"])
        self.assertNotIn(".work/", zip_upload["with"]["path"])
        self.assertEqual(zip_upload["with"]["if-no-files-found"], "error")
        self.assertIn("steps.package.outcome == 'success'", zip_upload["if"])
        self.assertLess(steps.index(stage), steps.index(compiled))
        self.assertLess(steps.index(compiled), steps.index(package_stage))
        self.assertLess(steps.index(package_stage), steps.index(zip_upload))
        self.assertLess(steps.index(next(s for s in steps if s.get("name") == "Save ccache immediately after compilation")),
                        steps.index(compiled))

    def test_actions_are_latest_stable_major_versions(self):
        for job in ("validate", "build"):
            steps = self.data["jobs"][job]["steps"]
            refs = [s["uses"] for s in steps if "uses" in s]
            self.assertIn("actions/checkout@v7", refs)
            self.assertIn("actions/setup-python@v7", refs)
            for ref in refs:
                self.assertNotIn("@v4", ref)
                self.assertNotIn("@v5", ref)


if __name__ == "__main__":
    unittest.main()
