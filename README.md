# visa-pickup-monitor

Check application numbers in the German Embassy in Belgrade's current visa pickup PDF.

The first milestone is a one-time local script. Each run discovers the PDF from the embassy page, downloads it, and prints whether each configured number appears in a valid pickup entry. Pickup dates do not affect matching. Read the linked PDF for pickup instructions.

## Run locally

Use Python 3.10 or newer. From the project directory, create a virtual environment and install the two dependencies:

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

Console output includes your configured application numbers; keep it private. This milestone stores no state: every run downloads the current PDF and prints all target results again. Telegram, deduplication, scheduling, and Healthchecks.io are planned in later phases.

On Windows, use `python` to create the environment, `Copy-Item .env.example .env` in PowerShell to copy the example, and `.venv\Scripts\python.exe` instead of `.venv/bin/python` for installation, execution, and tests.

## Verify

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests run offline with synthetic data and generated PDFs. The real embassy PDF is checked separately and is not stored in this repository.
