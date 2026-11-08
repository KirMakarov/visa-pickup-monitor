"""Check the German Embassy in Belgrade's current visa pickup list once."""

import argparse
import json
import os
import re
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pypdf import PdfReader

DEFAULT_PAGE_URL = "https://belgrad.diplo.de/rs-sr/service/2339474-2339474"
STATE_PATH = Path("data/state.json")

NUMBER_PATTERN = re.compile(r"[0-9]{6}")
ENTRY_PATTERN = re.compile(
    r"(?<!\w)([0-9]{6})\s+([0-9]{2}\.[0-9]{2}\.[0-9]{4})\s+"
    r"([0-9]{2}:[0-9]{2})\s*-\s*([0-9]{2}:[0-9]{2})(?![\w:])"
)

T = TypeVar("T")


class MonitorError(Exception):
    """A configuration, download, or pickup-list processing failure."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class TelegramConfig:
    token: str = field(repr=False)
    chat_id: str


@dataclass
class State:
    last_pdf_url: str | None = None
    last_processed_date: str | None = None
    last_targets: list[str] = field(default_factory=list)
    found_numbers: dict[str, str] = field(default_factory=dict)
    notified_numbers: list[str] = field(default_factory=list)
    error_notifications: list[str] = field(default_factory=list)


def read_settings(env_path: str | Path = ".env") -> dict[str, str | None]:
    try:
        return {**dotenv_values(env_path), **os.environ}
    except (OSError, UnicodeError) as exc:
        raise MonitorError("Could not read the .env file.") from exc


def load_telegram(settings: dict[str, str | None]) -> TelegramConfig | None:
    token = (settings.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (settings.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token and not chat_id:
        return None
    if not token or not chat_id:
        raise MonitorError(
            "Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or leave both empty."
        )
    if re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) is None:
        raise MonitorError("TELEGRAM_BOT_TOKEN has an invalid format.")
    return TelegramConfig(token, chat_id)


def load_config(settings: dict[str, str | None]) -> tuple[str, tuple[str, ...]]:
    page_url = (settings.get("PAGE_URL") or DEFAULT_PAGE_URL).strip()
    try:
        parsed_url = urlsplit(page_url)
    except ValueError as exc:
        raise MonitorError("PAGE_URL must be an absolute HTTP or HTTPS URL.") from exc
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise MonitorError("PAGE_URL must be an absolute HTTP or HTTPS URL.")

    numbers = tuple(
        number.strip() for number in (settings.get("NUMBERS") or "").split(",")
    )
    if any(NUMBER_PATTERN.fullmatch(number) is None for number in numbers):
        raise MonitorError(
            "Set NUMBERS to comma-separated six-digit application numbers."
        )
    return page_url, tuple(dict.fromkeys(numbers))


def load_state(path: Path) -> State:  # noqa: C901
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State()
    except (OSError, UnicodeError, ValueError) as exc:
        raise MonitorError(
            "Could not read state; restore data/state.json before retrying."
        ) from exc

    try:
        if not isinstance(data, dict):
            raise ValueError
        version = data.pop("version", None)
        if type(version) is not int or version not in (1, 2, 3):
            raise ValueError
        expected = set(asdict(State())) | ({"daily"} if version == 2 else set())
        if set(data) != expected:
            raise ValueError
        daily = data.pop("daily", None)
        if daily is not None:
            if not isinstance(daily, dict) or set(daily) != {
                "date",
                "successful",
                "failed",
                "pdf_downloaded",
                "errors",
                "report_delivered",
            }:
                raise ValueError
            if (
                not isinstance(daily["date"], str)
                or date.fromisoformat(daily["date"]).isoformat() != daily["date"]
            ):
                raise ValueError
            if any(
                type(daily[key]) is not int or daily[key] < 0
                for key in ("successful", "failed")
            ):
                raise ValueError
            if any(
                type(daily[key]) is not bool
                for key in ("pdf_downloaded", "report_delivered")
            ):
                raise ValueError
            if not isinstance(daily["errors"], list) or any(
                not isinstance(x, str) for x in daily["errors"]
            ):
                raise ValueError
        state = State(**data)
        for values in (state.last_targets, state.notified_numbers):
            if not isinstance(values, list) or any(
                not isinstance(number, str) or NUMBER_PATTERN.fullmatch(number) is None
                for number in values
            ):
                raise ValueError
        if not isinstance(state.found_numbers, dict) or any(
            not isinstance(number, str)
            or NUMBER_PATTERN.fullmatch(number) is None
            or not isinstance(url, str)
            or not url.startswith(("https://", "http://"))
            for number, url in state.found_numbers.items()
        ):
            raise ValueError
        if not set(state.notified_numbers) <= state.found_numbers.keys():
            raise ValueError
        if not isinstance(state.error_notifications, list) or any(
            not isinstance(message, str) for message in state.error_notifications
        ):
            raise ValueError
        if (state.last_pdf_url is None) != (state.last_processed_date is None):
            raise ValueError
        if state.last_pdf_url is not None:
            if not isinstance(
                state.last_pdf_url, str
            ) or not state.last_pdf_url.startswith(("https://", "http://")):
                raise ValueError
        if state.last_processed_date is not None:
            date.fromisoformat(state.last_processed_date)
        return state
    except (ValueError, TypeError) as exc:
        raise MonitorError(
            "Invalid state; restore data/state.json before retrying."
        ) from exc


def save_state(state: State, path: Path) -> None:
    data = {"version": 3, **asdict(state)}
    for name in ("last_targets", "notified_numbers", "error_notifications"):
        data[name] = sorted(data[name])
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    temporary = None
    try:
        if path.exists() and path.read_text(encoding="utf-8") == payload:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".state-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError) as exc:
        raise MonitorError("Could not save data/state.json.") from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def belgrade_date(now: datetime | None = None) -> str:
    try:
        return (
            (now or datetime.now(timezone.utc))
            .astimezone(ZoneInfo("Europe/Belgrade"))
            .date()
            .isoformat()
        )
    except ZoneInfoNotFoundError as exc:
        raise MonitorError(
            "Europe/Belgrade time zone data is missing; install tzdata."
        ) from exc


class _PickupLinkParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__()
        self.page_url = page_url
        self.links: set[str] = set()
        self.href: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.href is None:
            return
        label = " ".join(" ".join(self.label).casefold().split())
        url = urldefrag(urljoin(self.page_url, self.href))[0]
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.path.lower().endswith(".pdf")
            and ("abholliste" in label or "lista za preuzimanje viza" in label)
        ):
            self.links.add(url)
        self.href = None


def resolve_pdf_url(html: str, page_url: str) -> str:
    parser = _PickupLinkParser(page_url)
    try:
        parser.feed(html)
        parser.close()
    except ValueError as exc:
        raise MonitorError(
            "Could not parse the source page's pickup-list links."
        ) from exc
    if len(parser.links) != 1:
        raise MonitorError(
            f"Expected one pickup-list PDF link; found {len(parser.links)}. "
            "Check the source page."
        )
    return next(iter(parser.links))


def retry(operation: Callable[[], T]) -> T:
    for attempt in range(2):
        try:
            return operation()
        except MonitorError as exc:
            if exc.retry_after is None or exc.retry_after > 30:
                raise
            sleep(max(2**attempt, exc.retry_after))
    return operation()


def retry_delay(code: int, body: bytes = b"") -> float | None:
    if code == 429:
        try:
            value = json.loads(body)["parameters"]["retry_after"]
            if type(value) is int and value >= 0:
                return value
        except (ValueError, KeyError, TypeError):
            pass
        return 1
    return 1 if code == 408 or code >= 500 else None


def request_once(request: Request, action: str) -> tuple[bytes, str]:
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(), response.geturl()
    except HTTPError as exc:
        body = b""
        with exc:
            with suppress(OSError, HTTPException):
                body = exc.read()
        raise MonitorError(
            f"Could not {action} (HTTP {exc.code}).", retry_delay(exc.code, body)
        ) from exc
    except (URLError, OSError, HTTPException, ValueError) as exc:
        raise MonitorError(
            f"Could not {action}; check the connection and URL.", 1
        ) from exc


def fetch(url: str, label: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "visa-pickup-monitor/0.1"})
    return retry(lambda: request_once(request, f"download {label}"))


def send_telegram(config: TelegramConfig, text: str) -> None:
    request = Request(
        f"https://api.telegram.org/bot{config.token}/sendMessage",
        data=json.dumps({"chat_id": config.chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    def send() -> None:
        body, _ = request_once(request, "send Telegram notification")
        try:
            response = json.loads(body)
            if not isinstance(response, dict):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise MonitorError("Telegram returned an invalid response.", 1) from exc
        if response.get("ok") is not True:
            code = response.get("error_code", 0)
            delay = retry_delay(code, body) if type(code) is int else None
            raise MonitorError(
                "Telegram rejected the notification; check the bot and chat.", delay
            )

    retry(send)


def parse_numbers(text: str) -> set[str]:
    numbers = set()
    for number, pickup_date, start, end in ENTRY_PATTERN.findall(text):
        try:
            datetime.strptime(pickup_date, "%d.%m.%Y")
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
        except ValueError:
            continue
        numbers.add(number)
    return numbers


def extract_numbers(pdf: bytes) -> set[str]:
    try:
        texts = [page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages]
    except Exception as exc:
        raise MonitorError("Could not read the PDF or extract its text.") from exc

    numbers = set()
    for text in texts:
        numbers.update(parse_numbers(text))
    if not numbers:
        raise MonitorError(
            "The PDF contains no valid pickup entries; check its format."
        )
    return numbers


def check_pdf(
    page_url: str, targets: tuple[str, ...], state: State, today: str
) -> None:
    page, effective_page_url = fetch(page_url, "the source page")
    try:
        html = page.decode("utf-8")
    except UnicodeError as exc:
        raise MonitorError("The source page is not valid UTF-8 text.") from exc
    pdf_url = resolve_pdf_url(html, effective_page_url)
    if (
        pdf_url == state.last_pdf_url
        and today == state.last_processed_date
        and set(targets) <= set(state.last_targets)
    ):
        print("PDF unchanged; already processed today.")
        return

    pdf, effective_pdf_url = fetch(pdf_url, "the pickup-list PDF")
    numbers = extract_numbers(pdf)
    print(f"PDF: {effective_pdf_url}")
    print(f"Valid application numbers: {len(numbers)}")
    for target in targets:
        if target in numbers and target not in state.found_numbers:
            state.found_numbers[target] = effective_pdf_url
            print(f"{target}: found in pickup list")
        elif target not in state.found_numbers:
            print(f"{target}: not found in pickup list")
    state.last_pdf_url = pdf_url
    state.last_processed_date = today
    state.last_targets = list(targets)


def report_errors(
    errors: list[MonitorError],
    telegram: TelegramConfig | None,
    state: State | None,
    state_path: Path,
) -> None:
    for message in dict.fromkeys(str(error) for error in errors):
        print(f"Error: {message}", file=sys.stderr)
        if telegram is None or (
            state is not None and message in state.error_notifications
        ):
            continue
        try:
            send_telegram(telegram, f"Visa Pickup Monitor error:\n{message}")
        except MonitorError as exc:
            print(f"Error alert could not be sent: {exc}", file=sys.stderr)
            continue
        if state is not None:
            state.error_notifications.append(message)
            try:
                save_state(state, state_path)
            except MonitorError as exc:
                print(f"Error: {exc}", file=sys.stderr)


def main(state_path: Path = STATE_PATH) -> int:  # noqa: C901
    telegram = None
    state = None
    errors = []
    complete = False
    saving_failed = False
    try:
        settings = read_settings()
        telegram = load_telegram(settings)
        state = load_state(state_path)
        page_url, targets = load_config(settings)
        complete = set(targets) <= state.found_numbers.keys() and (
            telegram is None or set(targets) <= set(state.notified_numbers)
        )
        if complete:
            saving_failed = True
            save_state(state, state_path)
            saving_failed = False
            print("Monitoring complete for the current target numbers.")
            return 0
        today = belgrade_date()
        if not set(targets) <= state.found_numbers.keys():
            try:
                check_pdf(page_url, targets, state, today)
            except MonitorError as exc:
                errors.append(exc)

        # Persist discoveries before any delivery, then each successful delivery separately.
        saving_failed = True
        save_state(state, state_path)
        saving_failed = False
        if telegram is not None:
            for number in targets:
                if (
                    number not in state.found_numbers
                    or number in state.notified_numbers
                ):
                    continue
                try:
                    send_telegram(
                        telegram,
                        f"Application number {number} found in the pickup list.\n"
                        f"PDF: {state.found_numbers[number]}",
                    )
                except MonitorError as exc:
                    errors.append(exc)
                    break
                state.notified_numbers.append(number)
                saving_failed = True
                save_state(state, state_path)
                saving_failed = False

        if not errors:
            state.error_notifications.clear()
            complete = set(targets) <= state.found_numbers.keys() and (
                telegram is None or set(targets) <= set(state.notified_numbers)
            )
            if complete:
                print("Monitoring complete for the current target numbers.")
    except MonitorError as exc:
        errors.append(exc)

    if state is not None and not errors:
        try:
            save_state(state, state_path)
        except MonitorError as exc:
            saving_failed = True
            errors.append(exc)
    if errors:
        report_errors(errors, telegram, None if saving_failed else state, state_path)

    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.exit(main())
