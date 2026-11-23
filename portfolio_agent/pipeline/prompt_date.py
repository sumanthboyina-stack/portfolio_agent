"""
Today's date for every prompt.

A model knows nothing after its training data, so without a date it answers as if it were still then: the chat once said the next
Fed meeting was "December 18, 2023". Every prompt that asks a model about markets, rates, news or timing starts with this line,
computed when the prompt is built (US Central, the zone the batches and schedule use), so the answer is anchored to the real day.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Chicago")


def _now() -> datetime:
    return datetime.now(_TZ)


def today_line(now: datetime | None = None) -> str:
    """One paragraph: the date, and what a model must therefore not assume."""
    d = (now.astimezone(_TZ) if now is not None and now.tzinfo else now) or _now()
    return (f"Today's date: {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year} ({d.date().isoformat()}, US Central). "
            "Your own training knowledge ends before this date: do not state dates, interest rates, prices, central-bank meetings or "
            "market conditions from memory, and do not describe an earlier date as now. Use only the data supplied here or returned by "
            "tools, treat any date in that data as relative to today's date, and say so when something you need is missing.")


def with_today(prompt: str, now: datetime | None = None) -> str:
    """*prompt* with the date line in front."""
    return f"{today_line(now)}\n\n{prompt}"
