"""Verify runner updates without touching a real private checkout."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/update_runner.py"
SHA = "a" * 40
TEMPLATE_FILES = (
    ".github/workflows/monitor.yml",
    ".gitignore",
    "README.md",
    "config.env.example",
)
PRIVATE_FILES = {
    "config.env": b"NUMBERS=000000\n",
    ".env": b"private settings\n",
    "data/state.json": b"private state\n",
    "notes.txt": b"unrelated file\n",
}


def snapshot(root):
    entries = {}
    for path in Path(root).rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        name = relative.as_posix()
        if path.is_symlink():
            entries[name] = ("symlink", str(path.readlink()))
        elif path.is_file():
            entries[name] = ("file", path.read_bytes())
        else:
            entries[name] = ("directory",)
    return entries


def expected_templates(monitor_ref=SHA):
    files = {
        name: (PROJECT / "examples/github-actions" / name).read_bytes()
        for name in TEMPLATE_FILES
    }
    prefix, pin_and_rest = files[TEMPLATE_FILES[0]].split(b"  MONITOR_REF: ", 1)
    _, rest = pin_and_rest.split(b"\n", 1)
    files[TEMPLATE_FILES[0]] = (
        prefix + b"  MONITOR_REF: " + monitor_ref.lower().encode() + b"\n" + rest
    )
    return files


class RunnerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.git_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.runner = self.create_runner("runner with spaces")

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.runner,
            env=self.git_env,
            check=True,
            capture_output=True,
            text=True,
        )

    def create_runner(self, name):
        root = Path(self.temporary.name) / name
        root.mkdir()
        self.git("init", "--quiet", cwd=root)
        return root

    def commit_fixture(self):
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Updater Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Synthetic runner fixture",
        )

    def assert_rejected_without_writes(self, result, before, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(snapshot(Path(self.temporary.name)), before)
        self.assertIn(message, result.stderr)

    def copy_public_project(self):
        public = Path(self.temporary.name) / "public project copy"
        for name in (
            "scripts/update_runner.py",
            *(f"examples/github-actions/{path}" for path in TEMPLATE_FILES),
        ):
            destination = public / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT / name, destination)
        return public

    def template_pin(self, public):
        workflow = public / "examples/github-actions" / TEMPLATE_FILES[0]
        content = workflow.read_text()
        pin = next(
            line
            for line in content.splitlines(keepends=True)
            if line.startswith("  MONITOR_REF: ")
        )
        return workflow, content, pin

    def write_runner_files(self, files):
        for name, payload in files.items():
            target = self.runner / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    def seed_old_tracked_templates(self):
        self.write_runner_files(
            {
                name: b"# Previous runner template\n" + payload
                for name, payload in expected_templates("b" * 40).items()
            }
        )
        self.commit_fixture()

    def run_update(self, *extra, monitor_ref=SHA, script=SCRIPT):
        return subprocess.run(
            [
                sys.executable,
                str(script),
                str(self.runner),
                "--monitor-ref",
                monitor_ref,
                *extra,
            ],
            cwd=self.temporary.name,
            env=self.git_env,
            capture_output=True,
            text=True,
        )

    def test_updates_only_template_files_and_preserves_private_data(self):
        self.write_runner_files(PRIVATE_FILES)
        before = snapshot(self.runner)
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, payload in expected_templates().items():
            self.assertEqual((self.runner / name).read_bytes(), payload)
        after = snapshot(self.runner)
        self.assertEqual(
            set(after) - set(before),
            set(TEMPLATE_FILES) | {".github", ".github/workflows"},
        )
        self.assertEqual({name: after[name] for name in before}, before)

    def test_invalid_monitor_refs_are_rejected_before_writing(self):
        values = ("", "a" * 39, "a" * 41, "g" * 40, "main", "v1.0.0")
        for value in values:
            with self.subTest(monitor_ref=value):
                before = snapshot(Path(self.temporary.name))
                result = self.run_update(monitor_ref=value)
                self.assert_rejected_without_writes(
                    result, before, "40-character commit SHA"
                )

    def test_uppercase_and_mixed_case_monitor_refs_are_normalized(self):
        for index, value in enumerate(("ABCDEF0123" * 4, "aBcDeF0123" * 4)):
            with self.subTest(monitor_ref=value):
                self.runner = self.create_runner(f"runner ref {index}")
                result = self.run_update(monitor_ref=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (self.runner / TEMPLATE_FILES[0]).read_bytes(),
                    expected_templates(value)[TEMPLATE_FILES[0]],
                )

    def test_symlinked_file_is_rejected_without_touching_its_target(self):
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(b"outside sentinel\x00\xff")
        (self.runner / "README.md").symlink_to(outside)
        self.commit_fixture()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(
            result, before, "Refusing to update a symlink"
        )

    def test_symlinked_parent_is_rejected_without_touching_its_directory(self):
        outside = Path(self.temporary.name) / "outside directory"
        (outside / "workflows").mkdir(parents=True)
        (outside / "workflows/monitor.yml").write_bytes(b"outside workflow\x00\xff")
        (self.runner / ".github").symlink_to(outside, target_is_directory=True)
        self.commit_fixture()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(
            result, before, "Refusing to update a symlink"
        )

    def test_missing_template_pin_is_rejected_before_writing(self):
        public = self.copy_public_project()
        workflow, content, pin = self.template_pin(public)
        workflow.write_text(content.replace(pin, "", 1))
        before = snapshot(Path(self.temporary.name))
        result = self.run_update(script=public / "scripts/update_runner.py")
        self.assert_rejected_without_writes(
            result, before, "exactly one pinned MONITOR_REF"
        )

    def test_duplicate_template_pins_are_rejected_before_writing(self):
        public = self.copy_public_project()
        workflow, content, pin = self.template_pin(public)
        workflow.write_text(
            content.replace(pin, pin + f"  MONITOR_REF: {'b' * 40}\n", 1)
        )
        before = snapshot(Path(self.temporary.name))
        result = self.run_update(script=public / "scripts/update_runner.py")
        self.assert_rejected_without_writes(
            result, before, "exactly one pinned MONITOR_REF"
        )

    def test_non_git_destination_is_rejected_without_writing(self):
        self.runner = Path(self.temporary.name) / "not a checkout"
        self.runner.mkdir()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(
            result, before, "Could not inspect the destination Git repository"
        )

    def test_missing_destination_is_rejected_without_creating_it(self):
        self.runner = Path(self.temporary.name) / "missing runner"
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(
            result, before, "existing directory outside the public project"
        )

    def test_destination_inside_public_project_copy_is_rejected_without_writing(self):
        public = self.copy_public_project()
        self.runner = public / "runner with spaces"
        self.runner.mkdir()
        self.git("init", "--quiet")
        before = snapshot(Path(self.temporary.name))
        result = self.run_update(script=public / "scripts/update_runner.py")
        self.assert_rejected_without_writes(
            result, before, "existing directory outside the public project"
        )

    def test_directory_in_place_of_template_file_is_rejected_before_writing(self):
        (self.runner / "README.md").mkdir()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(result, before, "Expected a regular file")

    def test_existing_tracked_templates_are_updated_and_private_data_is_preserved(self):
        self.seed_old_tracked_templates()
        self.write_runner_files(PRIVATE_FILES)
        before = snapshot(self.runner)
        result = self.run_update(monitor_ref="c" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, payload in expected_templates("c" * 40).items():
            self.assertEqual((self.runner / name).read_bytes(), payload)
        after = snapshot(self.runner)
        self.assertEqual(set(after), set(before))
        self.assertEqual(
            {name: after[name] for name in before if name not in TEMPLATE_FILES},
            {name: before[name] for name in before if name not in TEMPLATE_FILES},
        )
        self.assertEqual(
            set(self.git("diff", "--name-only").stdout.splitlines()),
            set(TEMPLATE_FILES),
        )
        self.assertEqual(self.git("diff", "--cached", "--binary").stdout, "")

    def test_repeated_update_of_committed_current_templates_makes_no_changes(self):
        self.seed_old_tracked_templates()
        first = self.run_update()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.count("Updated:"), 4)
        self.commit_fixture()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(Path(self.temporary.name)), before)
        self.assertIn("already up to date", result.stdout)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_dry_run_preserves_existing_tracked_templates_and_inventory(self):
        self.seed_old_tracked_templates()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Would update:"), 4)
        self.assertEqual(snapshot(Path(self.temporary.name)), before)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_dry_run_writes_nothing(self):
        result = self.run_update("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Would update:"), 4)
        self.assertEqual([p.name for p in self.runner.iterdir()], [".git"])

    def test_local_template_changes_are_not_overwritten(self):
        target = self.runner / "README.md"
        target.write_text("Local edits\n")
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(result, before, "local changes")

    def test_modified_tracked_template_is_not_overwritten(self):
        self.seed_old_tracked_templates()
        (self.runner / "README.md").write_text("Local tracked edits\n")
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(result, before, "local changes")

    def test_deleted_tracked_template_is_not_recreated(self):
        self.seed_old_tracked_templates()
        (self.runner / "README.md").unlink()
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(result, before, "local changes")

    def test_staged_template_changes_are_preserved_in_worktree_and_index(self):
        self.seed_old_tracked_templates()
        (self.runner / "README.md").write_text("Staged local edits\n")
        self.git("add", "--", "README.md")
        before = snapshot(Path(self.temporary.name))
        staged = self.git("diff", "--cached", "--binary").stdout
        result = self.run_update()
        self.assert_rejected_without_writes(result, before, "local changes")
        self.assertEqual(self.git("diff", "--cached", "--binary").stdout, staged)

    def test_nested_checkout_directory_is_rejected(self):
        nested = self.runner / "nested"
        nested.mkdir()
        self.runner = nested
        before = snapshot(Path(self.temporary.name))
        result = self.run_update()
        self.assert_rejected_without_writes(
            result, before, "root of the runner checkout"
        )


if __name__ == "__main__":
    unittest.main()
