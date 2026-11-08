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
    def test_health_signal_follows_core_steps_and_excludes_manual_and_cancelled_runs(self):
        workflow = WORKFLOW.read_text()
        signal = workflow.split("      - name: Signal final hosted outcome\n", 1)[1]
        self.assertIn("!cancelled() && inputs.scheduled", signal)
        self.assertIn("JOB_STATUS: ${{ job.status }}", signal)
        self.assertIn("  workflow_dispatch:\n    inputs:", workflow)
        self.assertNotIn("  schedule:", workflow)
        for name in ("scheduled", "report"):
            self.assertIn(f"      {name}:\n", workflow)
        self.assertEqual(workflow.count("        type: boolean\n        default: false"), 2)
        self.assertLess(workflow.index("      - name: Save state after"), workflow.index("      - name: Signal final hosted outcome"))
        self.assertNotIn("/start", signal)

    def test_health_signals_final_outcome_and_rejects_failed_transport(self):
        script = step_script("Signal final hosted outcome")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "curl").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> calls\n'
                'printf "%s" "${FAKE_RESPONSE:-OK}"\n'
                'printf "\\n%s" "${FAKE_STATUS:-200}"\nexit "${CURL_EXIT:-0}"\n'
            )
            (root / "curl").chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                   "HEALTHCHECKS_PING_URL": "", "JOB_STATUS": "success"}

            def run():
                return subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True, text=True)

            self.assertEqual(run().returncode, 0)
            self.assertFalse((root / "calls").exists())
            env["HEALTHCHECKS_PING_URL"] = "https://health.example/synthetic-secret/"
            self.assertEqual(run().returncode, 0)
            self.assertTrue((root / "calls").read_text().strip().endswith("https://health.example/synthetic-secret"))
            for status in ("failure", "cancelled"):
                with self.subTest(status=status):
                    env["JOB_STATUS"] = status
                    self.assertEqual(run().returncode, 0)
                    self.assertTrue((root / "calls").read_text().strip().endswith("https://health.example/synthetic-secret/fail"))
            for changes in ({"FAKE_RESPONSE": "OK (not found)"}, {"FAKE_RESPONSE": "OK (rate limited)"},
                            {"FAKE_STATUS": "500"}, {"CURL_EXIT": "28"}):
                with self.subTest(changes=changes):
                    env.update(changes)
                    result = run()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("synthetic-secret", result.stdout + result.stderr)
                    for key in changes:
                        del env[key]

    def test_scheduled_lifecycle_signals_after_required_actions(self):
        scripts = [step_script(name) for name in (
            "Check the pickup list", "Save state after a successful or failed check",
            "Signal final hosted outcome")]
        cases = ("success", "completed", "monitor", "report", "commit", "push", "setup")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config.env").write_text("NUMBERS=123456\n")
                (root / "data").mkdir()
                (root / "data/state.json").write_text("{}")
                executables = {
                    "python": '#!/bin/sh\necho monitor >> calls\nexit "${MONITOR_EXIT:-0}"\n',
                    "git": '#!/bin/sh\necho "git $*" >> calls\n'
                           'if [ "$1" = diff ]; then exit 1; fi\n'
                           'if [ "$1" = -c ] && [ "$CASE" = commit ]; then exit 1; fi\n'
                           'if [ "$1" = push ] && [ "$CASE" = push ]; then exit 1; fi\n',
                    "curl": '#!/bin/sh\nfor arg; do url="$arg"; done\necho "health $url" >> calls\nprintf "OK\\n200"\n',
                }
                for name, contents in executables.items():
                    (root / name).write_text(contents)
                    (root / name).chmod(0o755)
                env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "CASE": case,
                       "DEFAULT_BRANCH": "main", "SCHEDULED": "true",
                       "REPORT": "true", "TELEGRAM_BOT_TOKEN": "",
                       "TELEGRAM_CHAT_ID": "", "HEALTHCHECKS_PING_URL": "https://health.example/check",
                       "MONITOR_EXIT": "1" if case in ("monitor", "report") else "0"}
                status = "success"
                if case == "setup":
                    status = "failure"
                else:
                    for script in scripts[:2]:
                        result = subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True)
                        if result.returncode:
                            status = "failure"
                env["JOB_STATUS"] = status
                result = subprocess.run(["bash", "-e", "-c", scripts[2]], cwd=root, env=env, capture_output=True)
                self.assertEqual(result.returncode, 0)
                calls = (root / "calls").read_text().splitlines()
                self.assertTrue(calls[-1].startswith("health "))
                self.assertEqual(calls[-1].endswith("/fail"), case not in ("success", "completed"))
                if case in ("monitor", "report"):
                    self.assertTrue(any(line.startswith("git push") for line in calls))

    def test_report_selected_by_explicit_scheduled_intent(self):
        script = step_script("Check the pickup list")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.env").write_text("NUMBERS=123456\n")
            (root / "python").write_text('#!/bin/sh\nprintf "%s\\n" "$*" > args\nexit "${MONITOR_EXIT:-0}"\n')
            (root / "python").chmod(0o755)
            for scheduled, report, expected in (("true", "true", True),
                                                 ("true", "false", False),
                                                 ("false", "false", False),
                                                 ("false", "true", False),
                                                 ("", "", False)):
                with self.subTest(scheduled=scheduled, report=report):
                    env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                           "SCHEDULED": scheduled, "REPORT": report,
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
