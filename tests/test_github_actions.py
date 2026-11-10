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

    def test_build_installs_and_persists_ccache(self):
        steps = self.data["jobs"]["build"]["steps"]
        install = next(s for s in steps if s.get("name") == "Install build dependencies and ccache")
        self.assertIn("ccache", install["run"])
        restore = next(s for s in steps if s.get("name") == "Restore ccache")
        self.assertEqual(restore["uses"], "actions/cache@v4")
        self.assertIn("CCACHE_DIR", restore["with"]["path"])
        self.assertIn("github.run_id", restore["with"]["key"])
        self.assertIn("github.run_attempt", restore["with"]["key"])
        self.assertIn("restore-keys", restore["with"])
        self.assertEqual(self.data["jobs"]["build"]["env"]["CCACHE_COMPILERCHECK"], "content")
        llvm = next(s for s in steps if s.get("name") == "Restore pinned LLVM toolchain")
        self.assertEqual(llvm["uses"], "actions/cache@v4")
        self.assertIn("steps.llvm.outputs.identity", llvm["with"]["key"])
        self.assertIn(".work/_toolchains/kernel/llvm", llvm["with"]["path"])

    def test_uploads_kernel_artifacts_and_shows_stats(self):
        steps = self.data["jobs"]["build"]["steps"]
        build = next(s for s in steps if s.get("name") == "Build selected kernel.yaml (DroidSpaces always enabled)")
        self.assertIn("python -m kernelforge build", build["run"])
        stats = next(s for s in steps if s.get("name") == "Report ccache performance")
        self.assertEqual(stats["if"], "always()")
        self.assertIn("GITHUB_STEP_SUMMARY", stats["run"])
        upload = next(s for s in steps if s.get("name") == "Upload compiled kernel images")
        self.assertEqual(upload["uses"], "actions/upload-artifact@v4")
        for path in ("Image", "dtb", "dtbo.img", ".config"):
            self.assertIn(path, upload["with"]["path"])


if __name__ == "__main__":
    unittest.main()
