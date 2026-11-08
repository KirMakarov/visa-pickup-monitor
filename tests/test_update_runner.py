"""Verify runner updates without touching a real private checkout."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/update_runner.py"
SHA = "a" * 40


class RunnerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runner = Path(self.temporary.name) / "runner with spaces"
        self.runner.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.runner)], check=True, capture_output=True)

    def run_update(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.runner), "--monitor-ref", SHA, *extra],
            cwd=self.temporary.name, capture_output=True, text=True,
        )

    def test_updates_only_template_files_and_preserves_private_data(self):
        private = {"config.env": b"NUMBERS=000000\n", ".env": b"private settings\n",
                   "data/state.json": b"private state\n"}
        for name, value in private.items():
            target = self.runner / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        workflow = (self.runner / ".github/workflows/monitor.yml").read_text()
        self.assertIn(f"  MONITOR_REF: {SHA}", workflow)
        self.assertIn("  workflow_dispatch:\n    inputs:", workflow)
        self.assertIn("!cancelled() && inputs.scheduled", workflow)
        self.assertNotIn("  schedule:", workflow)
        for name in (".gitignore", "README.md", "config.env.example"):
            self.assertEqual((self.runner / name).read_bytes(), (PROJECT / "examples/github-actions" / name).read_bytes())
        for name, value in private.items():
            self.assertEqual((self.runner / name).read_bytes(), value)

    def test_dry_run_writes_nothing(self):
        result = self.run_update("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Would update:"), 4)
        self.assertEqual([p.name for p in self.runner.iterdir()], [".git"])

    def test_local_template_changes_are_not_overwritten(self):
        target = self.runner / "README.md"
        target.write_text("Local edits\n")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local changes", result.stderr)
        self.assertEqual(target.read_text(), "Local edits\n")
        self.assertFalse((self.runner / ".github").exists())

    def test_nested_checkout_directory_is_rejected(self):
        nested = self.runner / "nested"
        nested.mkdir()
        self.runner = nested
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root of the runner checkout", result.stderr)
        self.assertEqual(list(nested.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
