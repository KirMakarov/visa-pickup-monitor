"""Exercise the public workflow's shell steps with synthetic commands."""

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / "examples/github-actions/.github/workflows/monitor.yml"
)


CHECK = "Check the pickup list"
SAVE = "Save state after a successful or failed check"
SIGNAL = "Signal final hosted outcome"


def step_definition(name):
    content = WORKFLOW.read_text()
    match = re.search(rf"(?m)^( *)- name: {re.escape(name)} *$", content)
    if match is None:
        raise AssertionError(f"Workflow step is missing: {name}")
    step = re.split(rf"(?m)^{match[1]}- ", content[match.end() :], maxsplit=1)[0]
    return textwrap.dedent(step).strip()


def step_script(name):
    definition = step_definition(name)
    script = re.search(r"(?m)^run: *\| *\n", definition)
    if script is None:
        raise AssertionError(f"Shell script is missing: {name}")
    return textwrap.dedent(definition[script.end() :])


def step_enabled(name, *, outcome="skipped", cancelled=False, scheduled=False):
    """Evaluate only the boolean vocabulary used by this workflow's two guards."""
    guard = re.search(r"(?m)^if: *\$\{\{ *(.*?) *\}\} *$", step_definition(name))
    if guard is None:
        raise AssertionError(f"Workflow guard is missing: {name}")
    expression = guard[1]
    for before, after in (
        ("cancelled()", repr(cancelled)),
        ("steps.check.outcome", repr(outcome)),
        ("inputs.scheduled", repr(scheduled)),
        ("&&", " and "),
        ("||", " or "),
        ("!", "not "),
    ):
        expression = expression.replace(before, after)
    tree = ast.parse(expression.strip(), mode="eval")
    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.Constant,
    )
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise AssertionError(f"Unsupported boolean guard: {name}")
    return eval(compile(tree, "<workflow guard>", "eval"), {"__builtins__": {}}, {})


def run_step(name, root, env):
    return subprocess.run(
        ["bash", "-e", "-c", step_script(name)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def run_guarded_step(name, root, env, **context):
    if step_enabled(name, **context):
        return run_step(name, root, env)
    return None


def prepare_workflow_checkout(root, case="success"):
    (root / "config.env").write_text("NUMBERS=123456\n")
    (root / ".env").write_text("NUMBERS=000000\n")
    (root / "data").mkdir()
    (root / "data/state.json").write_text("{}")
    (root / "monitor-code").mkdir()
    (root / "monitor-code/monitor.py").write_text(
        textwrap.dedent("""\
        import json, os, sys
        from pathlib import Path
        case = os.environ["CASE"]
        with Path("calls").open("a") as stream:
            stream.write(f"monitor {case}\\n")
        Path("probe.json").write_text(json.dumps({
            "config": Path(".env").read_text(),
            "token": os.environ.get("TELEGRAM_BOT_TOKEN"),
            "chat": os.environ.get("TELEGRAM_CHAT_ID"),
        }))
        Path("data/state.json").write_text(json.dumps({"synthetic_outcome": case}))
        print("Monitoring complete." if case == "completed" else "Synthetic check executed.")
        sys.exit(1 if case == "monitor" else 0)
    """)
    )
    executables = {
        "python": '#!/bin/sh\nprintf "%s\\n" "$@" > args\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        "git": '#!/bin/sh\necho "git $*" >> calls\n'
        'if [ "$1" = diff ]; then exit 1; fi\n'
        'if [ "$1" = -c ] && [ "$CASE" = commit ]; then exit 1; fi\n'
        'if [ "$1" = push ] && [ "$CASE" = push ]; then exit 1; fi\n',
        "curl": '#!/bin/sh\nfor arg; do url="$arg"; done\necho "health $url" >> calls\nprintf "OK\\n200"\n',
    }
    for name, contents in executables.items():
        (root / name).write_text(contents)
        (root / name).chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{root}:{os.environ['PATH']}",
        "CASE": case,
        "DEFAULT_BRANCH": "runner-default",
        "TELEGRAM_BOT_TOKEN": "123:workflow-test-token",
        "TELEGRAM_CHAT_ID": "-789",
        "HEALTHCHECKS_PING_URL": "https://health.example/check",
    }


class WorkflowTests(unittest.TestCase):
    def assert_setting(self, definition, key, expected, indent=0):
        values = re.findall(
            rf"(?m)^{' ' * indent}{re.escape(key)}: *(.*?) *$", definition
        )
        self.assertEqual(len(values), 1, f"Expected one workflow setting: {key}")
        self.assertEqual("".join(values[0].split()), "".join(expected.split()))

    def run_health_signal(
        self,
        *,
        status="success",
        health_url="https://health.example/synthetic-secret/",
        response="OK",
        http_status="200",
        curl_exit="0",
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            curl_log = Path(directory) / "curl.log"
            (root / "curl").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CURL_LOG"\n'
                'printf "%s\\n%s" "$FAKE_RESPONSE" "$FAKE_STATUS"\n'
                'exit "$CURL_EXIT"\n'
            )
            (root / "curl").chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "CURL_LOG": str(curl_log),
                "HEALTHCHECKS_PING_URL": health_url,
                "JOB_STATUS": status,
                "FAKE_RESPONSE": response,
                "FAKE_STATUS": http_status,
                "CURL_EXIT": curl_exit,
            }
            result = subprocess.run(
                ["bash", "-e", "-c", step_script(SIGNAL)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            calls = curl_log.read_text().splitlines() if curl_log.exists() else []
            return result, calls

    def test_health_signal_follows_core_steps_and_binds_final_job_status(self):
        workflow = WORKFLOW.read_text()
        signal = step_definition(SIGNAL)
        self.assert_setting(signal, "JOB_STATUS", "${{ job.status }}", indent=2)
        self.assert_setting(
            signal,
            "HEALTHCHECKS_PING_URL",
            "${{ secrets.HEALTHCHECKS_PING_URL }}",
            indent=2,
        )
        self.assertLess(
            workflow.index(f"- name: {CHECK}"), workflow.index(f"- name: {SAVE}")
        )
        self.assertLess(
            workflow.index(f"- name: {SAVE}"), workflow.index(f"- name: {SIGNAL}")
        )
        self.assertNotIn("/start", signal)

    def test_workflow_dispatch_has_one_optional_scheduled_flag(self):
        workflow = WORKFLOW.read_text()
        dispatch = re.search(r"(?ms)^ +workflow_dispatch: *\n(.*?)(?=^\S|\Z)", workflow)
        self.assertIsNotNone(dispatch)
        self.assertRegex(dispatch[1], r"(?m)^ +scheduled: *$")
        self.assertEqual(re.findall(r"(?m)^ +type: *(\S+)", dispatch[1]), ["boolean"])
        self.assertEqual(re.findall(r"(?m)^ +default: *(\S+)", dispatch[1]), ["false"])
        self.assertNotRegex(dispatch[1], r"(?m)^ +report:")
        self.assertNotRegex(workflow, r"(?m)^ *schedule:")

    def test_save_guard_allows_only_non_cancelled_success_and_failure(self):
        cases = (
            ("success", False, True),
            ("failure", False, True),
            ("skipped", False, False),
            ("cancelled", False, False),
            ("success", True, False),
            ("failure", True, False),
            ("skipped", True, False),
            ("cancelled", True, False),
        )
        for outcome, cancelled, expected in cases:
            with self.subTest(cancelled=cancelled, outcome=outcome):
                self.assertEqual(
                    step_enabled(SAVE, cancelled=cancelled, outcome=outcome),
                    expected,
                )

    def test_health_guard_allows_only_non_cancelled_scheduled_runs(self):
        for scheduled, cancelled, expected in (
            (True, False, True),
            (False, False, False),
            (True, True, False),
            (False, True, False),
        ):
            with self.subTest(scheduled=scheduled, cancelled=cancelled):
                self.assertEqual(
                    step_enabled(SIGNAL, scheduled=scheduled, cancelled=cancelled),
                    expected,
                )

    def test_setup_failure_skips_save_but_allows_scheduled_health_guard(self):
        # Model only the step outcomes after setup failed; no hosted setup runs here.
        self.assertFalse(step_enabled(SAVE, outcome="skipped"))
        self.assertTrue(step_enabled(SIGNAL, scheduled=True))

    def test_monitor_step_binds_current_telegram_secrets_and_check_outcome(self):
        check = step_definition(CHECK)
        self.assert_setting(check, "id", "check")
        for setting in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            self.assert_setting(
                check, setting, f"${{{{ secrets.{setting} }}}}", indent=2
            )
        self.assert_setting(
            step_definition(SAVE),
            "DEFAULT_BRANCH",
            "${{ github.event.repository.default_branch }}",
            indent=2,
        )

    def test_private_runner_uses_current_default_branch_and_shared_concurrency(self):
        workflow = WORKFLOW.read_text()
        self.assert_setting(
            workflow, "if", "${{ github.event.repository.private }}", indent=4
        )
        self.assert_setting(workflow, "contents", "write", indent=2)
        self.assert_setting(workflow, "group", "visa-pickup-monitor", indent=2)
        self.assert_setting(workflow, "cancel-in-progress", "false", indent=2)
        checkout = step_definition("Check out current private configuration and state")
        self.assertRegex(checkout, r"(?m)^uses: actions/checkout@\S+$")
        self.assertNotRegex(checkout, r"(?m)^  (repository|path):")
        self.assertNotRegex(checkout, r"(?m)^  persist-credentials: *false")
        self.assert_setting(
            checkout, "ref", "${{ github.event.repository.default_branch }}", indent=2
        )

    def test_public_monitor_checkout_uses_full_pin_and_expected_working_path(self):
        self.assertRegex(
            WORKFLOW.read_text(), r"(?m)^ +MONITOR_REF: *[0-9a-fA-F]{40} *$"
        )
        checkout = step_definition("Check out pinned public monitor code")
        self.assertRegex(checkout, r"(?m)^uses: actions/checkout@\S+$")
        self.assert_setting(
            checkout, "repository", "KirMakarov/visa-pickup-monitor", indent=2
        )
        self.assert_setting(checkout, "ref", "${{ env.MONITOR_REF }}", indent=2)
        self.assert_setting(checkout, "path", "monitor-code", indent=2)
        self.assert_setting(checkout, "persist-credentials", "false", indent=2)

    def test_health_signals_final_outcome_and_rejects_failed_transport(self):
        cases = (
            ("success", "OK", "200", "0", "https://health.example/synthetic-secret"),
            (
                "failure",
                "OK",
                "200",
                "0",
                "https://health.example/synthetic-secret/fail",
            ),
            (
                "success",
                "OK (not found)",
                "200",
                "0",
                "https://health.example/synthetic-secret",
            ),
            (
                "failure",
                "OK",
                "500",
                "0",
                "https://health.example/synthetic-secret/fail",
            ),
            (
                "success",
                "OK",
                "200",
                "28",
                "https://health.example/synthetic-secret",
            ),
        )
        for status, response, http_status, curl_exit, expected_url in cases:
            with self.subTest(
                status=status,
                response=response,
                http_status=http_status,
                curl_exit=curl_exit,
            ):
                result, calls = self.run_health_signal(
                    status=status,
                    response=response,
                    http_status=http_status,
                    curl_exit=curl_exit,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    calls[0],
                    "--silent --max-time 15 --write-out \\n%{http_code} "
                    + expected_url,
                )
                if (response, http_status, curl_exit) == ("OK", "200", "0"):
                    self.assertEqual(result.returncode, 0)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("synthetic-secret", result.stdout + result.stderr)
                    self.assertIn("Healthchecks", result.stderr)

        result, calls = self.run_health_signal(health_url="")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_cancelled_guard_and_direct_shell_signal_are_separate(self):
        self.assertFalse(step_enabled(SIGNAL, scheduled=True, cancelled=True))
        result, calls = self.run_health_signal(status="cancelled")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            calls[0].endswith("https://health.example/synthetic-secret/fail")
        )

    def test_scheduled_lifecycle_signals_after_required_actions(self):
        cases = (
            ("success", 0, 0),
            ("completed", 0, 0),
            ("monitor", 1, 0),
            ("commit", 0, 1),
            ("push", 0, 1),
        )
        for case, check_exit, save_exit in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = prepare_workflow_checkout(root, case)
                check = run_step(CHECK, root, env)
                self.assertEqual(check.returncode, check_exit)
                self.assertEqual(
                    "Monitoring complete." in check.stdout, case == "completed"
                )
                outcome = "failure" if check.returncode else "success"
                save = run_guarded_step(SAVE, root, env, outcome=outcome)
                self.assertIsNotNone(save, "Save guard suppressed persistence")
                self.assertEqual(save.returncode, save_exit)
                env["JOB_STATUS"] = (
                    "failure" if check.returncode or save.returncode else "success"
                )
                signal = run_guarded_step(SIGNAL, root, env, scheduled=True)
                self.assertIsNotNone(signal, "Health guard suppressed the signal")
                self.assertEqual(signal.returncode, 0)
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(calls[0], f"monitor {case}")
                self.assertEqual(calls[1], "git add -- data/state.json")
                self.assertEqual(
                    calls[-1],
                    "health https://health.example/check"
                    + ("/fail" if case in ("monitor", "commit", "push") else ""),
                )
                self.assertEqual(
                    [line for line in calls if line.startswith("git push")],
                    [] if case == "commit" else ["git push origin HEAD:runner-default"],
                )

    def test_scheduled_and_manual_runs_invoke_expected_monitor_cli(self):
        for scheduled in ("true", "false", ""):
            with (
                self.subTest(scheduled=scheduled),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                env = prepare_workflow_checkout(root)
                env["SCHEDULED"] = scheduled
                result = run_step(CHECK, root, env)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(
                    (root / "args").read_text().splitlines(),
                    ["-u", "monitor-code/monitor.py"],
                )
                self.assertIn("Synthetic check executed.", result.stdout)
                env["CASE"] = "monitor"
                self.assertEqual(run_step(CHECK, root, env).returncode, 1)

    def test_monitor_uses_current_configuration_and_telegram_settings_without_logging_values(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = prepare_workflow_checkout(root)
            for config in ("NUMBERS=111111\n", "NUMBERS=222222\n"):
                with self.subTest(config=config):
                    (root / "config.env").write_text(config)
                    result = run_step(CHECK, root, env)
                    self.assertEqual(result.returncode, 0)
                    probe = json.loads((root / "probe.json").read_text())
                    self.assertEqual(probe["config"], config)
                    for key, setting in (
                        ("token", "TELEGRAM_BOT_TOKEN"),
                        ("chat", "TELEGRAM_CHAT_ID"),
                    ):
                        self.assertTrue(
                            probe[key] == env[setting],
                            f"Monitor did not receive {setting}",
                        )
                        self.assertFalse(
                            env[setting] in result.stdout + result.stderr,
                            f"Workflow output exposes {setting}",
                        )

    def test_state_step_skips_missing_and_unchanged_and_propagates_push_failure(self):
        script = step_script("Save state after a successful or failed check")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "git").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> calls\nif [ "$1" = diff ]; then exit "${DIFF_EXIT:-0}"; fi\nif [ "$1" = push ]; then exit "${PUSH_EXIT:-0}"; fi\n'
            )
            (root / "git").chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "DEFAULT_BRANCH": "main",
            }

            def run():
                return subprocess.run(
                    ["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True
                ).returncode

            self.assertEqual(run(), 0)
            self.assertFalse((root / "calls").exists())
            (root / "data").mkdir()
            (root / "data/state.json").write_text("{}")
            self.assertEqual(run(), 0)
            unchanged_calls = (root / "calls").read_text().splitlines()
            self.assertFalse(any("commit" in line for line in unchanged_calls))
            self.assertFalse(any(line.startswith("push ") for line in unchanged_calls))
            env.update(DIFF_EXIT="1", PUSH_EXIT="1")
            self.assertNotEqual(run(), 0)
            changed_calls = (root / "calls").read_text().splitlines()
            self.assertTrue(any(" commit " in line for line in changed_calls))
            self.assertEqual(
                [line for line in changed_calls if line.startswith("push ")],
                ["push origin HEAD:main"],
            )

    def test_changed_state_stages_and_commits_state_without_unrelated_files(self):
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            for name, content in (
                ("data/state.json", "initial state\n"),
                ("config.env", "synthetic private config\n"),
                ("notes.txt", "initial notes\n"),
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            subprocess.run([real_git, "init", "-q"], cwd=root, check=True)
            subprocess.run(
                [real_git, "config", "user.name", "Workflow test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                [real_git, "config", "user.email", "workflow-test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run([real_git, "add", "--all"], cwd=root, check=True)
            subprocess.run(
                [real_git, "commit", "-qm", "initial fixture"], cwd=root, check=True
            )
            (root / "data/state.json").write_text("updated state\n")
            (root / "config.env").write_text("changed synthetic private config\n")
            (root / "notes.txt").write_text("changed notes\n")

            command_log = Path(directory) / "git-commands.log"
            staged_paths = Path(directory) / "staged-paths.txt"
            shim = Path(directory) / "git"
            shim.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$GIT_COMMAND_LOG"\n'
                'if [ "$1" = push ]; then exit "${PUSH_EXIT:-0}"; fi\n'
                "is_commit=0\n"
                'for arg; do [ "$arg" = commit ] && is_commit=1; done\n'
                'if [ "$is_commit" -eq 1 ]; then\n'
                '  "$REAL_GIT" diff --cached --name-only > "$STAGED_PATHS"\n'
                "fi\n"
                'exec "$REAL_GIT" "$@"\n'
            )
            shim.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "DEFAULT_BRANCH": "runner-default",
                "REAL_GIT": real_git,
                "GIT_COMMAND_LOG": str(command_log),
                "STAGED_PATHS": str(staged_paths),
            }
            result = run_step(SAVE, root, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(staged_paths.read_text().splitlines(), ["data/state.json"])
            committed_paths = subprocess.run(
                [real_git, "show", "--pretty=format:", "--name-only", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(committed_paths, ["data/state.json"])
            self.assertEqual(
                subprocess.run(
                    [real_git, "diff", "--cached", "--name-only"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.splitlines(),
                [],
            )
            self.assertEqual(
                subprocess.run(
                    [real_git, "diff", "--name-only"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.splitlines(),
                ["config.env", "notes.txt"],
            )
            commands = command_log.read_text().splitlines()
            self.assertEqual(
                [line for line in commands if line.startswith("push ")],
                ["push origin HEAD:runner-default"],
            )


if __name__ == "__main__":
    unittest.main()
