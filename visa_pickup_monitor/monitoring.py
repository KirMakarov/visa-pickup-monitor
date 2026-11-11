"""Coordinate one monitoring check and its error handling."""

import sys
from pathlib import Path

from visa_pickup_monitor.domain import State
from visa_pickup_monitor.infrastructure import (
    STATE_PATH,
    MonitorError,
    TelegramConfig,
    belgrade_date,
    extract_entries,
    fetch,
    load_broadcast,
    load_config,
    load_state,
    load_telegram,
    read_settings,
    resolve_pdf_url,
    save_state,
    send_telegram,
)


def check_pdf(
    page_url: str,
    targets: tuple[str, ...],
    state: State,
    today: str,
    broadcast: bool = False,
) -> None:
    page, effective_page_url = fetch(page_url, "the source page")
    try:
        html = page.decode("utf-8")
    except UnicodeError as exc:
        raise MonitorError("The source page is not valid UTF-8 text.") from exc
    pdf_url = resolve_pdf_url(html, effective_page_url)
    if (not broadcast or state.broadcast_baseline) and state.cache_applies(
        pdf_url, today, targets
    ):
        print("PDF unchanged; already processed today.")
        return

    pdf, effective_pdf_url = fetch(pdf_url, "the pickup-list PDF")
    entries = extract_entries(pdf)
    numbers = {pair[:6] for pair in entries}
    if broadcast and (
        not state.broadcast_baseline
        or state.last_pdf_url != pdf_url
        or state.last_processed_date != today
    ):
        new_entries = {
            pair: intervals
            for pair, intervals in entries.items()
            if pair not in state.broadcast_seen
        }
        if state.broadcast_baseline and new_entries:
            state.broadcast_pending.extend(
                broadcast_messages(new_entries, effective_pdf_url)
            )
        state.broadcast_seen.extend(new_entries)
        state.broadcast_baseline = True
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


def broadcast_messages(entries: dict[str, list[str]], pdf_url: str) -> list[str]:
    header = "New pickup dates:\n"
    footer = f"\nPDF: {pdf_url}"
    messages: list[str] = []
    current = header
    for pair, intervals in entries.items():
        for interval in intervals:
            line = f"{pair} {interval}\n"
            if len(current) + len(line) + len(footer) > 4096:
                if current == header:
                    raise MonitorError("PDF URL is too long for a Telegram message.")
                messages.append(current.rstrip() + footer)
                current = header
            current += line
    if current != header:
        messages.append(current.rstrip() + footer)
    return messages


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
        telegram_error = None
        try:
            telegram = load_telegram(settings)
        except MonitorError as exc:
            telegram_error = exc
        state = load_state(state_path)
        if (
            not (settings.get("TELEGRAM_BROADCAST_CHAT_ID") or "").strip()
            and state.broadcast_baseline
        ):
            state.broadcast_baseline = False
            saving_failed = True
            save_state(state, state_path)
            saving_failed = False
        if telegram_error is not None:
            raise telegram_error
        broadcast = load_broadcast(settings)
        page_url, targets = load_config(settings)
        complete = state.is_complete(targets, telegram is not None)
        if complete and broadcast is None:
            saving_failed = True
            save_state(state, state_path)
            saving_failed = False
            print("Monitoring complete for the current target numbers.")
            return 0
        today = belgrade_date()
        if broadcast is not None or not state.all_found(targets):
            try:
                check_pdf(page_url, targets, state, today, broadcast is not None)
            except MonitorError as exc:
                errors.append(exc)

        # Persist discoveries before any delivery, then each successful delivery separately.
        saving_failed = True
        save_state(state, state_path)
        saving_failed = False
        if telegram is not None:
            for number in targets:
                if not state.needs_delivery(number):
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

        if broadcast is not None:
            while state.broadcast_pending:
                try:
                    send_telegram(broadcast, state.broadcast_pending[0])
                except MonitorError as exc:
                    errors.append(exc)
                    break
                state.broadcast_pending.pop(0)
                saving_failed = True
                save_state(state, state_path)
                saving_failed = False

        if not errors:
            state.error_notifications.clear()
            complete = state.is_complete(targets, telegram is not None)
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
