# visa-pickup-monitor

Check application numbers in the German Embassy in Belgrade's current visa pickup PDF.

Run a local check with persistent history and optional Telegram notifications. While numbers remain to be found, each run discovers the current PDF from the embassy page. Pickup dates do not affect matching. Read the linked PDF for pickup instructions.

## Run locally

Use Python 3.10 or newer. From the project directory, create a virtual environment and install the dependencies (`pypdf`, `python-dotenv`, and `tzdata`):

```sh
python3 -m venv .venv
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

## State and repeated checks

The script creates `data/state.json` automatically. It contains private discovery and notification history and is ignored by Git. It is written atomically. Invalid or unreadable state causes an error instead of resetting history; restore a valid copy before retrying. Deleting state discards deduplication history and can repeat notifications.

- An unchanged PDF URL is downloaded and parsed once per calendar day in `Europe/Belgrade`. The source page is still checked on each active run.
- A changed PDF URL or a newly added target not covered by the last successful processing triggers parsing immediately. Content replaced under the same URL after successful processing is detected on the following day.
- Adding targets starts monitoring them; removing targets stops checking them while keeping their history. Console matches are printed only when first recorded.
- Once all current targets have been found and any required match notifications sent, the script reports completion and makes no source-page or PDF requests. Adding targets or enabling Telegram resumes the required work automatically.

## Run on GitHub Actions

Use the [private runner template](examples/github-actions/README.md) to run a pinned version of this code on GitHub Actions with private settings and persistent state. Its workflow supports the planned Belgrade schedule and manual runs. Copy the template into a separate private repository; the template workflow is inactive in this public repository.

In the private repository, configure both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as GitHub Actions repository secrets. The setup guide explains which template files to retain and how to update an existing runner.

Daily reports and Healthchecks.io are planned in later iterations.

## Verify

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests run offline with synthetic data and generated PDFs. The real embassy PDF is checked separately and is not stored in this repository.
