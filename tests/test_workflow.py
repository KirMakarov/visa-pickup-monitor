"""Exercise the public workflow's shell steps with synthetic commands."""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / "examples/github-actions/.github/workflows/monitor.yml"


def step_script(name):
    content = WORKFLOW.read_text()
    step = content.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class WorkflowTests(unittest.TestCase):
    def test_report_selected_by_scheduled_occurrence(self):
        script = step_script("Check the pickup list")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.env").write_text("NUMBERS=123456\n")
            (root / "python").write_text('#!/bin/sh\nprintf "%s\\n" "$*" > args\nexit "${MONITOR_EXIT:-0}"\n')
            (root / "python").chmod(0o755)
            for event, schedule, expected in (("schedule", "17 17 * * 1-4", True),
                                               ("schedule", "17 12-15 * * 1-4", False),
                                               ("workflow_dispatch", "", False)):
                with self.subTest(event=event, schedule=schedule):
                    env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                           "EVENT_NAME": event, "EVENT_SCHEDULE": schedule,
                           "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
                    result = subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual("--report" in (root / "args").read_text(), expected)
                    env["MONITOR_EXIT"] = "1"
                    self.assertNotEqual(subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True).returncode, 0)

    def test_state_step_skips_missing_and_unchanged_and_propagates_push_failure(self):
        script = step_script("Save state after a successful or failed check")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "git").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> calls\nif [ "$1" = diff ]; then exit "${DIFF_EXIT:-0}"; fi\nif [ "$1" = push ]; then exit "${PUSH_EXIT:-0}"; fi\n')
            (root / "git").chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "DEFAULT_BRANCH": "main"}
            def run():
                return subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True).returncode
            self.assertEqual(run(), 0)
            self.assertFalse((root / "calls").exists())
            (root / "data").mkdir()
            (root / "data/state.json").write_text("{}")
            self.assertEqual(run(), 0)
            self.assertNotIn("push", (root / "calls").read_text())
            env.update(DIFF_EXIT="1", PUSH_EXIT="1")
            self.assertNotEqual(run(), 0)
            self.assertIn("push origin HEAD:main", (root / "calls").read_text())


if __name__ == "__main__":
    unittest.main()
