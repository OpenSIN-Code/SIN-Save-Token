#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "sin_token_stack.py"
SPEC = importlib.util.spec_from_file_location("sin_token_stack", MODULE_PATH)
assert SPEC and SPEC.loader
STACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STACK)


class TokenOptimizerStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = STACK.load_config()

    def test_config_is_strict_and_all_sources_are_distinct_mit_pins(self) -> None:
        config = STACK.validate_config(json.loads(json.dumps(self.config)))
        upstreams = config["upstreams"]
        self.assertEqual(set(upstreams), {"ponytail", "pxpipe"})
        self.assertTrue(all(item["license"] == "MIT" for item in upstreams.values()))
        self.assertEqual(len({item["role"] for item in upstreams.values()}), 2)
        for spec in upstreams.values():
            commit = spec["assessed_commit"]
            self.assertRegex(commit, r"^[0-9a-f]{40}$")

    def test_config_rejects_unknown_sources_and_traversing_runtime_paths(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["upstreams"]["unexpected"] = config["upstreams"]["ponytail"]
        with self.assertRaisesRegex(STACK.StackError, "upstreams muss exakt"):
            STACK.validate_config(config)
        config = json.loads(json.dumps(self.config))
        config["upstreams"]["pxpipe"]["runtime_project"] = "../escape"
        with self.assertRaisesRegex(STACK.StackError, "Repositorys"):
            STACK.validate_config(config)

    def test_config_rejects_floating_package_versions_and_bad_sri(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["upstreams"]["pxpipe"]["npm_package"] = "pxpipe-proxy@latest"
        with self.assertRaisesRegex(STACK.StackError, "SemVer"):
            STACK.validate_config(config)
        config = json.loads(json.dumps(self.config))
        config["upstreams"]["pxpipe"]["transitive_integrity"] = {}
        with self.assertRaisesRegex(STACK.StackError, "gpt-tokenizer"):
            STACK.validate_config(config)

    def test_managed_home_rejects_filesystem_root(self) -> None:
        with mock.patch.dict(os.environ, {"SIN_TOKEN_STACK_HOME": "/"}):
            with self.assertRaisesRegex(STACK.StackError, "Root"):
                STACK.managed_home(self.config)

    def test_git_url_normalization_keeps_origin_check_stable(self) -> None:
        self.assertEqual(
            STACK.normalize_git_url("https://github.com/teamchong/pxpipe.git"),
            "https://github.com/teamchong/pxpipe",
        )

    def test_policy_is_explicit_and_unknown_models_fail_closed(self) -> None:
        policy = self.config["policy"]
        self.assertIn("visual-context-compression", policy["explicit_only"])
        self.assertEqual(policy["pxpipe"]["default_mode"], "off")
        self.assertNotIn("gigatoken", policy)
        allowed, reason = STACK.pxpipe_policy("gpt-5.6-terra", True, self.config)
        self.assertFalse(allowed)
        self.assertEqual(reason, "model is not allowlisted")

    def test_safe_and_lossy_model_policies(self) -> None:
        self.assertEqual(
            STACK.pxpipe_policy("claude-fable-5", False, self.config),
            (True, "validated-default"),
        )
        self.assertEqual(
            STACK.pxpipe_policy("gpt-5.6-sol", False, self.config),
            (False, "model requires --accept-lossy"),
        )
        self.assertEqual(
            STACK.pxpipe_policy("gpt-5.6-sol", True, self.config),
            (True, "lossy-opt-in"),
        )

    def test_provider_routing_is_separate_from_compression_allowlist(self) -> None:
        self.assertEqual(STACK.resolve_pxpipe_route("gpt-5.6-sol"), "openai")
        self.assertEqual(STACK.resolve_pxpipe_route("claude-fable-5"), "default")
        self.assertEqual(STACK.resolve_pxpipe_route("kimi-k2"), "cloudflare")
        env: dict[str, str] = {}
        STACK.configure_pxpipe_routing(env, "gpt-5.6-sol", "openai")
        self.assertEqual(env["OPENAI_MODELS"], "gpt-5.6-sol")
        self.assertNotIn("CLOUDFLARE_MODELS", env)

    def test_never_image_precision_categories_exist(self) -> None:
        blocked = set(self.config["policy"]["pxpipe"]["never_image"])
        self.assertTrue(
            {
                "secrets",
                "credentials",
                "hashes",
                "opaque-identifiers",
                "patch-anchors",
                "exact-error-strings",
                "byte-exact-protocol-state",
            }
            <= blocked
        )

    def test_chunk_planning_math_and_guards(self) -> None:
        self.assertEqual(STACK.planned_chunks(0, 1000, 100), 0)
        self.assertEqual(STACK.planned_chunks(800, 1000, 100), 1)
        self.assertEqual(STACK.planned_chunks(2500, 1000, 100), 3)
        self.assertIsNone(STACK.planned_chunks(2500, None, 0))
        for size, overlap in ((0, 0), (100, -1), (100, 100)):
            with self.assertRaises(STACK.StackError):
                STACK.planned_chunks(100, size, overlap)

    def test_pxpipe_committed_runtime_is_exactly_locked(self) -> None:
        runtime, version = STACK.validate_pxpipe_runtime_project(self.config)
        self.assertEqual(version, "0.10.0")
        lock = json.loads((runtime / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(
            lock["packages"]["node_modules/pxpipe-proxy"]["integrity"],
            self.config["upstreams"]["pxpipe"]["npm_integrity"],
        )
        self.assertEqual(
            lock["packages"]["node_modules/gpt-tokenizer"]["integrity"],
            self.config["upstreams"]["pxpipe"]["transitive_integrity"][
                "gpt-tokenizer@3.4.0"
            ],
        )

    def test_pxpipe_runtime_rejects_integrity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            (runtime / "package.json").write_text(
                json.dumps({"dependencies": {"pxpipe-proxy": "0.10.0"}}),
                encoding="utf-8",
            )
            lock = json.loads(
                (ROOT / "runtime" / "pxpipe" / "package-lock.json").read_text()
            )
            lock["packages"]["node_modules/pxpipe-proxy"]["integrity"] = (
                "sha512-tampered"
            )
            (runtime / "package-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )
            with mock.patch.object(STACK, "_safe_project_path", return_value=runtime):
                with self.assertRaisesRegex(STACK.StackError, "SRI"):
                    STACK.validate_pxpipe_runtime_project(self.config)

    def test_pxpipe_argv_never_falls_back_to_global_or_npx(self) -> None:
        with (
            mock.patch.object(
                STACK,
                "pxpipe_runtime_state",
                return_value={"ready": False, "binary": "/tmp/not-ready"},
            ),
            mock.patch.object(STACK.shutil, "which", return_value="/usr/local/bin/npx"),
        ):
            with self.assertRaisesRegex(STACK.StackError, "sync --source pxpipe"):
                STACK.pxpipe_argv(self.config)

    def test_compatible_node_skips_legacy_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_bin = root / "old"
            new_bin = root / "new"
            old_bin.mkdir()
            new_bin.mkdir()
            old_node = old_bin / "node"
            new_node = new_bin / "node"
            old_node.write_text("#!/bin/sh\necho v16.6.2\n", encoding="utf-8")
            new_node.write_text("#!/bin/sh\necho v24.16.0\n", encoding="utf-8")
            old_node.chmod(0o755)
            new_node.chmod(0o755)
            with mock.patch.dict(
                os.environ, {"PATH": f"{old_bin}{os.pathsep}{new_bin}"}
            ):
                self.assertEqual(STACK.compatible_node(), new_node.resolve())

    def test_pxpipe_argv_binds_explicit_compatible_node(self) -> None:
        with (
            mock.patch.object(
                STACK,
                "pxpipe_runtime_state",
                return_value={"ready": True, "binary": "/managed/pxpipe/bin/cli.js"},
            ),
            mock.patch.object(
                STACK, "compatible_node", return_value=Path("/opt/node-v24")
            ),
        ):
            self.assertEqual(
                STACK.pxpipe_argv(self.config),
                ["/opt/node-v24", "/managed/pxpipe/bin/cli.js"],
            )

    def test_pxpipe_runtime_install_is_locked_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text(
                json.dumps({"name": "pxpipe-proxy", "version": "0.10.0"}),
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            node = fake_bin / "node"
            node.write_text("#!/bin/sh\necho v20.11.0\n", encoding="utf-8")
            npm = fake_bin / "npm"
            npm.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "mkdir -p node_modules/pxpipe-proxy/bin node_modules/.bin\n"
                'printf \'%s\\n\' \'{"name":"pxpipe-proxy","version":"0.10.0"}\' > node_modules/pxpipe-proxy/package.json\n'
                "printf '%s\\n' '#!/bin/sh' 'exit 0' > node_modules/pxpipe-proxy/bin/cli.js\n"
                "chmod +x node_modules/pxpipe-proxy/bin/cli.js\n"
                "ln -s ../pxpipe-proxy/bin/cli.js node_modules/.bin/pxpipe\n",
                encoding="utf-8",
            )
            node.chmod(0o755)
            npm.chmod(0o755)
            env = {
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "SIN_TOKEN_STACK_HOME": str(root / "managed"),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(STACK, "ensure_assessed_source", return_value=source),
            ):
                first = STACK.install_pxpipe_runtime(self.config)
                self.assertEqual(first["action"], "installed")
                self.assertTrue(STACK.pxpipe_runtime_state(self.config)["ready"])
                second = STACK.install_pxpipe_runtime(self.config)
                self.assertEqual(second["action"], "verified")

    def test_pxpipe_runtime_failed_install_preserves_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text(
                json.dumps({"name": "pxpipe-proxy", "version": "0.10.0"}),
                encoding="utf-8",
            )
            managed = root / "managed" / ".runtime" / "pxpipe"
            managed.mkdir(parents=True)
            marker_file = managed / "keep.txt"
            marker_file.write_text("preserve", encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            node = fake_bin / "node"
            node.write_text("#!/bin/sh\necho v20.11.0\n", encoding="utf-8")
            npm = fake_bin / "npm"
            npm.write_text("#!/bin/sh\necho failed >&2\nexit 7\n", encoding="utf-8")
            node.chmod(0o755)
            npm.chmod(0o755)
            env = {
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "SIN_TOKEN_STACK_HOME": str(root / "managed"),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(STACK, "ensure_assessed_source", return_value=source),
            ):
                with self.assertRaisesRegex(STACK.StackError, "npm ci"):
                    STACK.install_pxpipe_runtime(self.config)
            self.assertEqual(marker_file.read_text(encoding="utf-8"), "preserve")

    def test_source_state_treats_unsynced_as_optional_and_symlink_as_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "managed"
            with mock.patch.dict(os.environ, {"SIN_TOKEN_STACK_HOME": str(home)}):
                state = STACK.source_state("ponytail", self.config)
                self.assertFalse(state["present"])
                self.assertFalse(state["ready"])
                home.mkdir()
                (home / "ponytail").symlink_to(Path(temp))
                state = STACK.source_state("ponytail", self.config)
                self.assertTrue(state["present"])
                self.assertFalse(state["installed"])

    def test_sync_one_clones_pins_verifies_and_rejects_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=source, check=True
            )
            (source / "README.md").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "one"], cwd=source, check=True)
            first = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True
            ).strip()
            (source / "README.md").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "two"], cwd=source, check=True)
            home = root / "managed"
            spec = {"url": str(source), "assessed_commit": first}
            result = STACK.sync_one("fixture", spec, home)
            self.assertEqual(result["commit"], first[:7])
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=home / "fixture", text=True
                ).strip(),
                first,
            )
            self.assertEqual(
                STACK.sync_one("fixture", spec, home)["action"], "verified"
            )
            (home / "fixture" / "dirty.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaisesRegex(STACK.StackError, "verändert"):
                STACK.sync_one("fixture", spec, home)

    def test_sync_one_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "managed"
            home.mkdir()
            (home / "fixture").symlink_to(root)
            with self.assertRaisesRegex(STACK.StackError, "Symlink"):
                STACK.sync_one(
                    "fixture",
                    {
                        "url": "https://github.com/example/example.git",
                        "assessed_commit": "a" * 40,
                    },
                    home,
                )

    def test_atomic_state_write_rejects_symlink_and_leaves_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "state.json"
            STACK._atomic_write_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})
            path.unlink()
            path.symlink_to(root / "target.json")
            with self.assertRaisesRegex(STACK.StackError, "Symlink"):
                STACK._atomic_write_json(path, {"ok": False})

    def test_sync_lock_fails_closed_when_already_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with STACK.sync_lock(home):
                with self.assertRaisesRegex(STACK.StackError, "bereits"):
                    with STACK.sync_lock(home):
                        self.fail("unreachable")

    def test_fresh_status_check_is_green_but_drifted_present_source_is_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "managed"
            args = argparse.Namespace(json=True, check=True)
            with (
                mock.patch.dict(os.environ, {"SIN_TOKEN_STACK_HOME": str(home)}),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(STACK.cmd_status(args), 0)
                self.assertTrue(json.loads(stdout.getvalue())["check_ok"])
            home.mkdir()
            (home / "ponytail").mkdir()
            with (
                mock.patch.dict(os.environ, {"SIN_TOKEN_STACK_HOME": str(home)}),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(STACK.cmd_status(args), 2)

    def test_retired_optimizer_commands_are_not_exposed(self) -> None:
        parser = STACK.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("token-count", help_text)
        self.assertNotIn("token-bench", help_text)
        self.assertNotIn("memory-compress", help_text)
        sync = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["sync"]
        source = next(a for a in sync._actions if a.dest == "source")
        self.assertEqual(set(source.choices), {"all", "ponytail", "pxpipe"})

    def test_pxpipe_export_validates_path_and_flag_combinations(self) -> None:
        args = argparse.Namespace(git=True, stdin=False, path="other")
        with (
            mock.patch.object(STACK, "load_config", return_value=self.config),
            mock.patch.object(STACK, "pxpipe_argv", return_value=["pxpipe"]),
        ):
            with self.assertRaisesRegex(STACK.StackError, "kombiniert"):
                STACK.cmd_pxpipe_export(args)
        args = argparse.Namespace(git=False, stdin=False, path="/definitely/missing")
        with (
            mock.patch.object(STACK, "load_config", return_value=self.config),
            mock.patch.object(STACK, "pxpipe_argv", return_value=["pxpipe"]),
        ):
            with self.assertRaisesRegex(STACK.StackError, "nicht gefunden"):
                STACK.cmd_pxpipe_export(args)

    def test_installer_and_verifier_expose_fail_closed_cli_without_auto_sync(
        self,
    ) -> None:
        installer = (ROOT / "bin" / "install.sh").read_text(encoding="utf-8")
        verifier = (ROOT / "bin" / "verify-tokens").read_text(encoding="utf-8")
        self.assertIn('ln -sfn "$REPO_DIR/bin/sin-token-stack"', installer)
        self.assertNotIn("sin-token-stack sync", installer)
        self.assertIn("sin-token-stack status --check --json", verifier)

    def test_main_reports_expected_failures_with_exit_two(self) -> None:
        with (
            mock.patch.object(
                STACK, "load_config", side_effect=STACK.StackError("broken")
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(STACK.main(["status"]), 2)
            self.assertIn("broken", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
