import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from kernelforge import cli


class LLVMToolchainTests(unittest.TestCase):
    def test_yaml_has_exactly_one_pinned_llvm_toolchain(self):
        cfg = cli.load_config()
        tc = cfg["toolchain"]
        self.assertEqual(set(tc), {"url", "ref", "commit"})
        self.assertEqual(tc["ref"], "lineage-20.0")
        self.assertEqual(tc["commit"], "54220fd601050b350b2af7adc913089ebf0e7aed")
        self.assertIn("clang-r416183b", tc["url"])

    def test_llvm_override_is_single_complete_directory(self):
        cfg = cli.load_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for tool in cli.LLVM_TOOLS:
                (root / tool).touch()
            with patch.dict(os.environ, {"KERNELFORGE_LLVM_BIN": directory}):
                self.assertEqual(cli.bootstrap_toolchain(cli.WORK_NAME, cfg), root.resolve())
            (root / "ld.lld").unlink()
            with patch.dict(os.environ, {"KERNELFORGE_LLVM_BIN": directory}):
                with self.assertRaisesRegex(cli.ForgeError, "ld.lld"):
                    cli.bootstrap_toolchain(cli.WORK_NAME, cfg)

    def test_llvm_environment_removes_gcc_cross_prefixes(self):
        cfg = cli.load_config()
        with patch.dict(os.environ, {
            "CROSS_COMPILE": "broken-",
            "CROSS_COMPILE_ARM32": "broken-arm-",
            "CLANG_TRIPLE": "broken-triple",
        }):
            env = cli.build_environment(cfg, Path("/opt/llvm/bin"))
        self.assertEqual(env["LLVM"], "1")
        self.assertEqual(env["LLVM_IAS"], "1")
        self.assertEqual(env["ARCH"], "arm64")
        self.assertEqual(env["PATH"].split(os.pathsep)[0], "/opt/llvm/bin")
        for name in ("CROSS_COMPILE", "CROSS_COMPILE_ARM32", "CLANG_TRIPLE"):
            self.assertNotIn(name, env)

    def test_config_and_compile_share_llvm_flags_and_path(self):
        cfg = cli.load_config()
        calls = []

        def record(argv, **kwargs):
            calls.append((argv, kwargs))
            return ""

        with patch.object(cli, "prepare", return_value=Path("/src")), \
             patch.object(cli, "bootstrap_toolchain", return_value=Path("/opt/llvm/bin")), \
             patch.object(cli, "run", side_effect=record), \
             redirect_stdout(io.StringIO()):
            cli.build(cli.WORK_NAME, cfg, 4, dry=True)

        self.assertEqual(len(calls), 3)
        for args, kwargs in calls[:2]:
            self.assertIn("LLVM=1", args)
            self.assertIn("LLVM_IAS=1", args)
            self.assertNotIn("CROSS_COMPILE=", " ".join(args))
            self.assertIn("CC=ccache clang", args)
            self.assertIn("HOSTCC=ccache clang", args)
            self.assertIn("HOSTCXX=ccache clang++", args)
            self.assertEqual(kwargs["env"]["PATH"].split(os.pathsep)[0], "/opt/llvm/bin")
        self.assertEqual(calls[1][0][-1], "-j4")
        self.assertEqual(calls[2][0], ["ccache", "--show-stats"])
        self.assertEqual(calls[1][1]["env"]["CCACHE_MAXSIZE"], "2G")
        self.assertIn("vendor/droidspaces.config", calls[0][0])

    def test_ccache_disabled_uses_plain_llvm(self):
        cfg = cli.load_config()
        cfg["build"]["ccache"]["enabled"] = False
        calls = []

        def record(argv, **kwargs):
            calls.append((argv, kwargs))
            return ""

        with (
            patch.object(cli, "prepare", return_value=Path("/src")),
            patch.object(cli, "bootstrap_toolchain", return_value=Path("/opt/llvm/bin")),
            patch.object(cli, "run", side_effect=record),
            redirect_stdout(io.StringIO()),
        ):
            cli.build(cli.WORK_NAME, cfg, 4, dry=True)

        self.assertEqual(len(calls), 2)
        for args, kwargs in calls:
            self.assertNotIn("CC=ccache clang", args)
            self.assertNotIn("CCACHE_MAXSIZE", kwargs["env"])

    def test_ccache_config_validation(self):
        cfg = cli.load_config()
        self.assertEqual(cfg["build"]["ccache"], {"enabled": True, "max_size": "2G"})
        cfg["build"]["ccache"]["enabled"] = "true"
        with self.assertRaises(cli.ForgeError):
            cli.validate_item(cfg["build"]["ccache"], {"enabled", "max_size"}, "build.ccache")


if __name__ == "__main__":
    unittest.main()