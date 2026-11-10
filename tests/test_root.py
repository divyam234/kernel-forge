import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest

from kernelforge.root import RootError, prepare_bakasu


def run(argv, cwd=None, env=None, dry=False, capture=False):
    if dry:
        print("+", " ".join(map(str, argv)))
        return ""
    result = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, text=True,
                            check=True, capture_output=True)
    return result.stdout.strip() if capture else ""


class RootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote = self.base / "upstream"
        run(["git", "init", "--initial-branch=main", self.remote])
        run(["git", "-C", self.remote, "config", "user.name", "Root fixture"])
        run(["git", "-C", self.remote, "config", "user.email", "root@example.invalid"])
        (self.remote / "kernel").mkdir()
        (self.remote / "kernel" / "Kconfig").write_text('config KSU\n\tbool "Root"\n')
        (self.remote / "kernel" / "Kbuild").write_text("obj-$(CONFIG_KSU) += fixture.o\n")
        (self.remote / "kernel" / "fixture.c").write_text("/* pinned fixture */\n")
        self.commit("pinned root")
        self.pin = self.rev(self.remote)
        run(["git", "-C", self.remote, "tag", "v0.1"])
        (self.remote / "kernel" / "fixture.c").write_text("/* newer branch tip */\n")
        self.commit("advance upstream")
        self.spec = {"url": str(self.remote), "ref": "main", "commit": self.pin}
        self.source = self.base / "source"
        drivers = self.source / "drivers"
        drivers.mkdir(parents=True)
        (drivers / "Makefile").write_text("obj-y += base/\n")
        (drivers / "Kconfig").write_text('menu "Device Drivers"\n\nsource "drivers/base/Kconfig"\n\nendmenu\n')

    def commit(self, message):
        run(["git", "-C", self.remote, "add", "."])
        run(["git", "-C", self.remote, "commit", "-m", message])

    def rev(self, repository):
        return run(["git", "-C", repository, "rev-parse", "HEAD"], capture=True)

    def test_pinned_checkout_and_real_driver_integration(self):
        self.assertEqual(prepare_bakasu(self.source, self.spec, run), self.pin)
        checkout = self.source / "KernelSU"
        self.assertTrue((checkout / ".git").is_dir())
        self.assertEqual(self.rev(checkout), self.pin)
        self.assertNotEqual(self.rev(self.remote), self.pin)
        self.assertEqual(run(["git", "-C", checkout, "rev-parse", "--is-shallow-repository"], capture=True), "false")
        self.assertEqual(run(["git", "-C", checkout, "rev-parse", "v0.1"], capture=True), self.pin)
        link = self.source / "drivers" / "kernelsu"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../KernelSU/kernel")
        self.assertEqual((link / "Kbuild").read_text(), (self.remote / "kernel" / "Kbuild").read_text())
        self.assertEqual((checkout / "kernel" / "fixture.c").read_text(), "/* pinned fixture */\n")
        make = (self.source / "drivers" / "Makefile").read_text()
        self.assertEqual(make.count("obj-$(CONFIG_KSU) += kernelsu/"), 1)
        config = (self.source / "drivers" / "Kconfig").read_text()
        self.assertEqual(config.count('source "drivers/kernelsu/Kconfig"'), 1)
        self.assertLess(config.index('source "drivers/kernelsu/Kconfig"'), config.rindex("endmenu"))

    def test_reprepare_cleans_generated_files_and_restores_pin(self):
        prepare_bakasu(self.source, self.spec, run)
        checkout = self.source / "KernelSU"
        generated = checkout / "kernel" / "generated.h"
        generated.write_text("generated\n")
        (checkout / "kernel" / "fixture.c").write_text("dirty\n")
        original_make = (self.source / "drivers" / "Makefile").read_bytes()
        original_config = (self.source / "drivers" / "Kconfig").read_bytes()
        self.assertEqual(prepare_bakasu(self.source, self.spec, run), self.pin)
        self.assertFalse(generated.exists())
        self.assertEqual(self.rev(checkout), self.pin)
        self.assertEqual((checkout / "kernel" / "fixture.c").read_text(), "/* pinned fixture */\n")
        self.assertEqual((self.source / "drivers" / "Makefile").read_bytes(), original_make)
        self.assertEqual((self.source / "drivers" / "Kconfig").read_bytes(), original_config)
        # Outer-kernel resets remove these entries but preserve the nested clone.
        (self.source / "drivers" / "Makefile").write_text("obj-y += base/\n")
        (self.source / "drivers" / "Kconfig").write_text('menu "Device Drivers"\nendmenu\n')
        (self.source / "drivers" / "kernelsu").unlink()
        prepare_bakasu(self.source, self.spec, run)
        self.assertTrue((self.source / "drivers" / "kernelsu" / "Kconfig").is_file())
        self.assertIn("CONFIG_KSU", (self.source / "drivers" / "Makefile").read_text())

    def test_sha_mismatch_rejects_integration(self):
        def mismatched(argv, **kwargs):
            result = run(argv, **kwargs)
            return "0" * 40 if argv[-2:] == ["rev-parse", "HEAD"] else result

        with self.assertRaisesRegex(RootError, "SHA mismatch"):
            prepare_bakasu(self.source, self.spec, mismatched)
        self.assertFalse((self.source / "drivers" / "kernelsu").is_symlink())
        self.assertNotIn("CONFIG_KSU", (self.source / "drivers" / "Makefile").read_text())

    def test_missing_pin_never_falls_back_to_branch(self):
        with self.assertRaises(RootError):
            prepare_bakasu(self.source, {**self.spec, "commit": "1" * 40}, run)
        self.assertFalse((self.source / "drivers" / "kernelsu").is_symlink())
        self.assertNotIn("CONFIG_KSU", (self.source / "drivers" / "Makefile").read_text())

    def test_conflicting_driver_directory_is_preserved(self):
        conflict = self.source / "drivers" / "kernelsu"
        conflict.mkdir()
        sentinel = conflict / "unrelated.c"
        sentinel.write_text("keep me\n")
        with self.assertRaisesRegex(RootError, "Conflicting"):
            prepare_bakasu(self.source, self.spec, run)
        self.assertEqual(sentinel.read_text(), "keep me\n")
        self.assertFalse((self.source / "KernelSU").exists())

    def test_conflicting_driver_symlink_is_preserved(self):
        conflict = self.source / "drivers" / "kernelsu"
        conflict.symlink_to("../unrelated", target_is_directory=True)
        with self.assertRaisesRegex(RootError, "Conflicting"):
            prepare_bakasu(self.source, self.spec, run)
        self.assertEqual(os.readlink(conflict), "../unrelated")

    def test_root_symlink_is_rejected_without_touching_target(self):
        (self.source / "KernelSU").symlink_to(self.remote, target_is_directory=True)
        before = self.rev(self.remote)
        with self.assertRaisesRegex(RootError, "symlinked"):
            prepare_bakasu(self.source, self.spec, run)
        self.assertEqual(self.rev(self.remote), before)

    def test_plain_directory_and_worktree_metadata_are_rejected(self):
        checkout = self.source / "KernelSU"
        checkout.mkdir()
        (checkout / "sentinel").write_text("keep\n")
        with self.assertRaisesRegex(RootError, "plain .git"):
            prepare_bakasu(self.source, self.spec, run)
        (checkout / ".git").write_text(f"gitdir: {self.remote / '.git'}\n")
        with self.assertRaisesRegex(RootError, "plain .git"):
            prepare_bakasu(self.source, self.spec, run)
        self.assertEqual((checkout / "sentinel").read_text(), "keep\n")

    def test_foreign_origin_is_rejected_before_cleanup(self):
        prepare_bakasu(self.source, self.spec, run)
        checkout = self.source / "KernelSU"
        run(["git", "-C", checkout, "remote", "set-url", "origin", str(self.base / "other")])
        sentinel = checkout / "untracked"
        sentinel.write_text("preserve\n")
        with self.assertRaisesRegex(RootError, "origin"):
            prepare_bakasu(self.source, self.spec, run)
        self.assertEqual(sentinel.read_text(), "preserve\n")

    def test_unknown_kconfig_layout_is_rejected_before_clone(self):
        (self.source / "drivers" / "Kconfig").write_text('source "drivers/base/Kconfig"\n')
        with self.assertRaisesRegex(RootError, "layout"):
            prepare_bakasu(self.source, self.spec, run)
        self.assertFalse((self.source / "KernelSU").exists())

    def test_missing_upstream_build_file_rejects_integration(self):
        (self.remote / "kernel" / "Kbuild").unlink()
        self.commit("remove required Kbuild")
        with self.assertRaisesRegex(RootError, "kernel/Kbuild"):
            prepare_bakasu(self.source, {**self.spec, "commit": self.rev(self.remote)}, run)
        self.assertFalse((self.source / "drivers" / "kernelsu").is_symlink())

    def test_origin_validation_accepts_git_url_rewrites(self):
        prepare_bakasu(self.source, self.spec, run)
        checkout = self.source / "KernelSU"
        run(["git", "-C", checkout, "config",
             f"url.{self.remote.as_uri()}.insteadOf", str(self.remote)])
        self.assertNotEqual(
            run(["git", "-C", checkout, "remote", "get-url", "origin"], capture=True),
            self.spec["url"])
        self.assertEqual(prepare_bakasu(self.source, self.spec, run), self.pin)
        self.assertEqual(self.rev(checkout), self.pin)

    def test_shallow_existing_clone_is_expanded_to_full_history(self):
        checkout = self.source / "KernelSU"
        # file:// makes Git honor --depth for a local deterministic fixture.
        url = self.remote.as_uri()
        run(["git", "clone", "--depth=1", "--single-branch", "--branch", "main", url, checkout])
        self.assertEqual(run(["git", "-C", checkout, "rev-parse", "--is-shallow-repository"], capture=True), "true")
        self.assertEqual(prepare_bakasu(self.source, {**self.spec, "url": url}, run), self.pin)
        self.assertEqual(run(["git", "-C", checkout, "rev-parse", "--is-shallow-repository"], capture=True), "false")
        self.assertEqual(self.rev(checkout), self.pin)

    def test_dry_run_of_existing_checkout_preserves_dirty_files(self):
        prepare_bakasu(self.source, self.spec, run)
        checkout = self.source / "KernelSU"
        tracked = checkout / "kernel" / "fixture.c"
        generated = checkout / "kernel" / "generated.h"
        tracked.write_text("dirty\n")
        generated.write_text("generated\n")
        make = (self.source / "drivers" / "Makefile").read_bytes()
        config = (self.source / "drivers" / "Kconfig").read_bytes()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(prepare_bakasu(self.source, self.spec, run, dry=True), self.pin)
        self.assertEqual(tracked.read_text(), "dirty\n")
        self.assertEqual(generated.read_text(), "generated\n")
        self.assertEqual((self.source / "drivers" / "Makefile").read_bytes(), make)
        self.assertEqual((self.source / "drivers" / "Kconfig").read_bytes(), config)

    def test_dry_run_succeeds_before_kernel_fetch_without_writes(self):
        nonexistent = self.base / "not-fetched" / "kernel"
        calls = []

        def dry_run(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertTrue(kwargs.get("dry"))
            return run(argv, **kwargs)

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(prepare_bakasu(nonexistent, self.spec, dry_run, dry=True), self.pin)
        self.assertFalse(nonexistent.parent.exists())
        self.assertTrue(calls)
        self.assertTrue(output.getvalue())


if __name__ == "__main__":
    unittest.main()
