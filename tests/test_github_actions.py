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


    @staticmethod
    def step_enabled(step, *, device, package, images="skipped", success=True, cancelled=False):
        """Evaluate the status/device expressions used by publication steps."""
        condition = step.get("if", "${{ success() }}").removeprefix("${{").removesuffix("}}")
        for token, value in (
            ("matrix.device", device),
            ("steps.package.outcome", package),
            ("steps.images.outcome", images),
            ("success()", success),
            ("always()", True),
            ("cancelled()", cancelled),
        ):
            condition = condition.replace(token, repr(value))
        condition = condition.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
        return eval(condition.strip(), {"__builtins__": {}}, {})

    def test_capri_generation_requires_successful_package_and_preparation(self):
        steps = self.data["jobs"]["build"]["steps"]
        cache = next(s for s in steps if s.get("name") == "Cache pinned Capri LineageOS originals and tools")
        prepare = next(s for s in steps if s.get("name") == "Prepare verified Capri LineageOS originals and native tools")
        images = next(s for s in steps if s.get("id") == "images")
        package = next(s for s in steps if s.get("id") == "package")
        self.assertLess(steps.index(package), steps.index(cache))
        self.assertLess(steps.index(cache), steps.index(prepare))
        self.assertLess(steps.index(prepare), steps.index(images))
        for step in (cache, prepare, images):
            with self.subTest(step=step["name"]):
                self.assertTrue(self.step_enabled(step, device="capri", package="success"))
                for device, package_outcome, prior_success in (
                    ("lemonades", "success", True),
                    ("capri", "failure", False),
                    ("capri", "skipped", False),
                    ("capri", "success", False),  # e.g. provisioning failed
                ):
                    self.assertFalse(self.step_enabled(
                        step, device=device, package=package_outcome, success=prior_success))

    def test_only_complete_capri_images_are_published_and_oneplus_is_unchanged(self):
        steps = self.data["jobs"]["build"]["steps"]
        uploads = [s for s in steps if s.get("uses") == "actions/upload-artifact@v7"]
        compiled = next(s for s in uploads if "dist/compiled/" in s["with"]["path"])
        images = next(s for s in uploads if "dist/images/" in s["with"]["path"])
        oneplus = next(s for s in uploads if "dist/packages/" in s["with"]["path"])
        self.assertEqual(len(uploads), 3)
        self.assertEqual(set(images["with"]["path"].splitlines()), {
            "dist/images/capri/boot.img", "dist/images/capri/vendor_boot.img",
            "dist/images/capri/vendor.img", "dist/images/capri/manifest.json",
        })
        self.assertEqual(images["with"]["if-no-files-found"], "error")
        self.assertIn("steps.package.outputs.artifact", oneplus["with"]["name"])
        self.assertIn("steps.package.outputs.filename", oneplus["with"]["path"])
        self.assertLess(steps.index(compiled), steps.index(next(s for s in steps if s.get("id") == "images")))
        for device in ("capri", "lemonades"):
            for outcome in ("success", "failure", "skipped"):
                with self.subTest(device=device, images=outcome):
                    self.assertEqual(
                        self.step_enabled(images, device=device, package="success", images=outcome),
                        device == "capri" and outcome == "success",
                    )
            self.assertEqual(
                self.step_enabled(oneplus, device=device, package="success"),
                device == "lemonades",
            )
        self.assertFalse(self.step_enabled(images, device="capri", package="success",
                                           images="failure", success=False))
        self.assertFalse(self.step_enabled(images, device="capri", package="success",
                                           images="success", cancelled=True))
        for outcome in ("failure", "skipped"):
            self.assertFalse(self.step_enabled(oneplus, device="lemonades", package=outcome,
                                               success=False))
        self.assertFalse(self.step_enabled(oneplus, device="lemonades", package="success",
                                           cancelled=True))



if __name__ == "__main__":
    unittest.main()
