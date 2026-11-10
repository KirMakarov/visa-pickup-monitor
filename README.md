# visa-pickup-monitor

[![CI](https://github.com/KirMakarov/visa-pickup-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/KirMakarov/visa-pickup-monitor/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/downloads/)

Check application numbers in the German Embassy in Belgrade's current visa pickup PDF.

Run a local check with persistent history and optional Telegram notifications. While numbers remain to be found, each run discovers the current PDF from the embassy page. Pickup dates do not affect matching. Read the linked PDF for pickup instructions.

## Run locally

Use Python 3.14 or newer. From the project directory, create a virtual environment with Python 3.14 and install the dependencies (`pypdf`, `python-dotenv`, and `tzdata`):

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and replace `NUMBERS` with your six-digit application numbers, separated by commas. Keep leading zeros. The `.env` file is ignored by Git. `PAGE_URL` defaults to the embassy page if omitted. Environment variables override `.env` values.

```sh
.venv/bin/python monitor.py
```

Example output with synthetic numbers:

```text
PDF: https://belgrad.diplo.de/example/pickup.pdf
Valid application numbers: 65
123456: found in pickup list
123457: not found in pickup list
```

Exit code `0` means the list was successfully checked, including when no targets match. Exit code `1` means configuration, download, or processing failed; the error is printed to stderr. Missing or ambiguous PDF links, unreadable PDFs, and PDFs without valid pickup entries are errors.

Console output includes your configured application numbers; keep it private. Run one instance at a time with the same state file.

On Windows, use `python` to create the environment, `Copy-Item .env.example .env` in PowerShell to copy the example, and `.venv\Scripts\python.exe` instead of `.venv/bin/python` for installation, execution, and tests.

## Optional Telegram notifications

Set both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` to enable notifications. Leave both empty to use the console only; incomplete configuration is an error. Create a bot with BotFather, send your bot a message, then use the official [getUpdates method](https://core.telegram.org/bots/api#getupdates) to find your `message.chat.id`.

Each match notification includes the application number and its discovery PDF link. Delivery is recorded only after Telegram confirms success. Temporary network and server failures receive up to three total attempts, with short delays. Telegram rate limits are respected; waits longer than 30 seconds defer delivery to a later run. Failed match notifications remain pending and are retried on subsequent runs, even if the source page is unavailable.

Enabling Telegram after local discoveries sends previously found, unnotified numbers from the current `NUMBERS` list. Successfully notified numbers are not sent again, including after a change of destination chat. An interruption between actual delivery and saving its confirmation can result in a duplicate.

Errors are printed to stderr and also sent to Telegram when possible. An identical error is notified once until a successful run clears suppression; there is no recovery message. Error alerts are not queued when Telegram is unavailable. If state cannot be read or saved, error-alert suppression cannot be persisted either.

Successful checks without new or pending personal notifications send no routine Telegram message.

## State and repeated checks

The script creates `data/state.json` automatically. It contains private discovery and notification history and is ignored by Git. It is written atomically. Invalid or unreadable state causes an error instead of resetting history; restore a valid copy before retrying. Deleting state discards deduplication history and can repeat notifications.
Recognized version-1 and version-2 state upgrades automatically, retaining discovery and notification history, error suppression, and cache eligibility. Report-only fields are discarded.

- An unchanged PDF URL is downloaded and parsed once per calendar day in `Europe/Belgrade`. The source page is still checked on each active run.
- A changed PDF URL or a newly added target not covered by the last successful processing triggers parsing immediately. Content replaced under the same URL after successful processing is detected on the following day.
- Adding targets starts monitoring them; removing targets stops checking them while keeping their history. Console matches are printed only when first recorded.
- Once all current targets have been found and any required match notifications sent, the script reports completion and makes no source-page or PDF requests. Adding targets or enabling Telegram resumes the required work automatically.

## Run on GitHub Actions

Use the [private runner template](examples/github-actions/README.md) to run a pinned version of this code on GitHub Actions with private settings and persistent state. cron-job.org triggers its Belgrade schedule, and its workflow also supports manual runs. Copy the template into a separate private repository; the template workflow is inactive in this public repository.

In the private repository, configure both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as GitHub Actions repository secrets. The setup guide explains which template files to retain and how to update an existing runner.

All scheduled checks behave alike. The runner can optionally signal scheduled outcomes and detect missed runs through Healthchecks.io; see the private runner guide for configuration and rollout.

## Verify

The launcher is `monitor.py`. The `visa_pickup_monitor` package separates monitoring history
(`domain.py`), technical operations (`infrastructure.py`), and the monitoring
check (`monitoring.py`). Application tests are grouped by configuration, source,
monitoring, state, and Telegram; runner and workflow tests remain separate.

Install the development tools in the same Python 3.14 virtual environment:

```sh
. .venv/bin/activate
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the same checks and tests as CI:

```sh
.venv/bin/pre-commit run --all-files --show-diff-on-failure
.venv/bin/python -m mypy
.venv/bin/python -m unittest discover -s tests -v
```

The checks cover Python linting, import sorting and formatting, complexity, YAML syntax, Markdown style, trailing whitespace, and file endings. Markdown line length is not limited. Mypy checks the application and scripts in strict mode; tests are excluded. Ruff also requires return annotations for special methods in application code. Some pre-commit checks fix files automatically; review their changes and run the checks again. Three existing functions are exempt from the complexity limit of 10.

Optionally install the Git hook to check staged files before each commit:

```sh
.venv/bin/pre-commit install
```

The first run downloads the pinned hook versions. Activate the virtual environment before running pre-commit or committing, so the mypy hook uses the installed project dependencies. Mypy checks all application code even when only some files are staged. Tests are not run by the hook; they run separately and in CI. Tests run offline with synthetic data and generated PDFs. The real embassy PDF is checked separately and is not stored in this repository.

On Windows, activate the virtual environment with `.venv\Scripts\Activate.ps1` in PowerShell and use `.venv\Scripts\pre-commit.exe` for the pre-commit commands above.
