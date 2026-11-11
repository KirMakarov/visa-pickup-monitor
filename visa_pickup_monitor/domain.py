"""Monitoring history and its decisions."""

from dataclasses import dataclass, field


@dataclass
class State:
    last_pdf_url: str | None = None
    last_processed_date: str | None = None
    last_targets: list[str] = field(default_factory=list)
    found_numbers: dict[str, str] = field(default_factory=dict)
    notified_numbers: list[str] = field(default_factory=list)
    error_notifications: list[str] = field(default_factory=list)

    broadcast_baseline: bool = False
    broadcast_seen: list[str] = field(default_factory=list)
    broadcast_pending: list[str] = field(default_factory=list)

    def all_found(self, targets: tuple[str, ...]) -> bool:
        return set(targets) <= self.found_numbers.keys()

    def is_complete(self, targets: tuple[str, ...], delivery_required: bool) -> bool:
        return self.all_found(targets) and (
            not delivery_required or set(targets) <= set(self.notified_numbers)
        )

    def cache_applies(self, pdf_url: str, today: str, targets: tuple[str, ...]) -> bool:
        return (
            pdf_url == self.last_pdf_url
            and today == self.last_processed_date
            and set(targets) <= set(self.last_targets)
        )

    def needs_delivery(self, number: str) -> bool:
        return number in self.found_numbers and number not in self.notified_numbers
