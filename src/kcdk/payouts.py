"""Strict, Decimal-based DraftKings payout schedules (amounts are gross)."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import csv
import re


@dataclass(frozen=True)
class PayoutTier:
    place_from: int
    place_to: int
    payout_cents: int


def _integer(text: str) -> int:
    text = text.strip().replace(",", "")
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"Invalid integer in payout schedule: {text!r}")
    return int(text)


def _money(text: str) -> int:
    try:
        value = Decimal(text.strip().replace("$", "").replace(",", ""))
        if not value.is_finite() or value < 0:
            raise ValueError("Payouts must be finite and nonnegative")
        return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("Invalid payout amount") from exc


def load_payouts(path: str | Path) -> tuple[PayoutTier, ...]:
    """Read normalized tiers or the DK alternating place/amount export."""
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        rows = [[cell.strip() for cell in row] for row in csv.reader(source)
                if row and any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("Payout schedule is empty")
    tiers = []
    if rows[0] == ["place_from", "place_to", "payout_cents"]:
        for row in rows[1:]:
            if len(row) != 3:
                raise ValueError("Payout rows require three columns")
            tiers.append(PayoutTier(*map(_integer, row)))
    else:
        if len(rows) % 2 or any(len(row) != 1 for row in rows):
            raise ValueError("Expected alternating place/range and dollar payout rows")
        for index in range(0, len(rows), 2):
            place = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", rows[index][0], flags=re.I)
            bounds = re.split(r"\s*[-–]\s*", place)
            if len(bounds) not in (1, 2):
                raise ValueError("Invalid payout place range")
            tiers.append(PayoutTier(_integer(bounds[0]), _integer(bounds[-1]), _money(rows[index + 1][0])))
    if not tiers:
        raise ValueError("Payout schedule has no tiers")
    tiers.sort(key=lambda tier: (tier.place_from, tier.place_to))
    previous_end = 0
    for tier in tiers:
        if tier.place_from < 1 or tier.place_to < tier.place_from or tier.payout_cents < 0:
            raise ValueError("Invalid/reversed payout range or negative payout")
        if tier.place_from <= previous_end:
            raise ValueError("Overlapping or duplicate payout tiers")
        previous_end = tier.place_to
    return tuple(tiers)


def payout_for_rank(rank: int, tiers: tuple[PayoutTier, ...] | None) -> int | None:
    if rank < 1:
        raise ValueError("Rank must be positive")
    if tiers is None:
        return None
    return next((tier.payout_cents for tier in tiers
                 if tier.place_from <= rank <= tier.place_to), 0)
