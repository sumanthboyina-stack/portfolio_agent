"""
CUSIP / ISIN -> ticker, for fund holdings that a provider identifies only by security identifier (SEC N-PORT does).

Source: OpenFIGI's mapping API (https://api.openfigi.com/v3/mapping, POST a list of {"idType": "ID_CUSIP" | "ID_ISIN", "idValue": ...}; one
result per job: {"data": [listings]}, {"warning": "No identifier found."} or {"error": ...}).

Rules
  * ONLY public security identifiers leave the machine. Nothing about any portfolio, owner or holding amount is ever sent.
  * The API key is optional and comes only from the OPENFIGI_API_KEY environment variable (header X-OPENFIGI-APIKEY); it is never stored,
    logged or put in an error message. Without a key OpenFIGI's lower limits apply (10 identifiers a request, 25 requests a minute), and the
    request spacing and batch size follow that; with one, 100 a request and 25 requests per 6 seconds.
  * Results are cached in security_identifiers, including "not found", and looked up again only after portfolio_settings.mapping_refresh_days.
    A transient failure (HTTP 429 / 5xx, timeout, malformed answer) is NOT cached: the identifier is simply tried again next time, and
    the run stops calling OpenFIGI after the first such failure.
  * A run looks up at most openfigi_max_ids_per_refresh NEW identifiers (callers pass the largest holdings first). Whatever is left stays
    unmapped, keyed by its CUSIP, and is picked up by a later refresh as the cache fills.
  * No database transaction is open during a network call; each batch's results are stored in their own short transaction.
  * Nothing guesses: an identifier with no listing stays unmapped. Tests never touch OpenFIGI (a fake session serves canned answers).
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools.fund_data_providers import RC_HOLDINGS_UNMAPPED, FundData

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
NO_KEY_MAX_BATCH, KEY_MAX_BATCH = 10, 100
NO_KEY_SPACING, KEY_SPACING = 60.0 / 25, 6.0 / 25          # seconds between requests: 25 a minute / 25 per 6 seconds

_CUSIP = re.compile(r"^[A-Z0-9]{9}$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_ID_TYPE = {"CUSIP": "ID_CUSIP", "ISIN": "ID_ISIN"}
_US_COMPOSITE = "US"


class MappingError(Exception):
    """A transient OpenFIGI failure: nothing is cached for the affected identifiers."""


def normalize_id(id_type: str, value: str | None) -> str | None:
    """The identifier upper-cased, or None when it is blank / "N/A" / not shaped like a CUSIP (9) or ISIN (12)."""
    v = (value or "").strip().upper()
    if not v or v in ("N/A", "NA", "NONE"):
        return None
    return v if (_CUSIP if id_type == "CUSIP" else _ISIN).match(v) else None


# ── Client ────────────────────────────────────────────────────────────────────

class _Spacing:
    def __init__(self, interval: float, *, clock=time.monotonic, sleep=time.sleep):
        self._i, self._clock, self._sleep, self._next, self._lock = interval, clock, sleep, 0.0, threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self._i
        if start > now:
            self._sleep(start - now)


_spacings: dict[float, _Spacing] = {}
_spacings_lock = threading.Lock()


def _shared_spacing(interval: float) -> _Spacing:
    with _spacings_lock:
        return _spacings.setdefault(interval, _Spacing(interval))


class OpenFigiClient:
    """POSTs a batch of jobs. `session` is injectable (tests pass a fake with .post())."""

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0, session=None, spacing: _Spacing | None = None):
        self._key = (os.getenv("OPENFIGI_API_KEY", "") if api_key is None else api_key).strip()
        self._timeout, self._session = timeout, session
        self._spacing = spacing or _shared_spacing(KEY_SPACING if self._key else NO_KEY_SPACING)

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    @property
    def max_batch(self) -> int:
        return KEY_MAX_BATCH if self._key else NO_KEY_MAX_BATCH

    def map(self, jobs: list[dict]) -> list[dict]:
        """One result dict per job, in order. Raises MappingError on any transient failure."""
        session = self._session
        if session is None:
            import requests
            session = requests
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["X-OPENFIGI-APIKEY"] = self._key
        self._spacing.wait()
        try:
            r = session.post(OPENFIGI_URL, json=jobs, headers=headers, timeout=self._timeout)
        except Exception as exc:                                  # never include the headers / key
            raise MappingError(f"request failed: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise MappingError(f"HTTP {r.status_code}")
        try:
            out = r.json()
        except Exception:
            raise MappingError("answer is not JSON") from None
        if not isinstance(out, list) or len(out) != len(jobs):
            raise MappingError("answer does not match the request")
        return out


def pick_listing(listings: list[dict]) -> dict | None:
    """The listing to use for an identifier: the US composite one when there is one, else the first with a ticker."""
    usable = [l for l in listings if isinstance(l, dict) and (l.get("ticker") or "").strip()]
    if not usable:
        return None
    for l in usable:
        if (l.get("exchCode") or "").upper() == _US_COMPOSITE:
            return l
    return usable[0]


# ── Resolution ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh(row: dict, now: datetime, days: float) -> bool:
    try:
        t = datetime.fromisoformat(row["resolved_at"])
    except Exception:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return now - t < timedelta(days=days)


def resolve_identifiers(refs: list[tuple[str, str]], cfg: dict, *, client: OpenFigiClient | None = None,
                        now: datetime | None = None) -> dict[tuple[str, str], str | None]:
    """
    {(id_type, id_value): ticker or None} for every well-formed identifier in `refs` (malformed ones are left out). A stored mapping that
    is still fresh is used as is; anything else is looked up (when cfg["openfigi_enabled"]) up to cfg["openfigi_max_ids_per_refresh"]
    new identifiers, in the order given. Never raises: an OpenFIGI failure leaves the affected identifiers unresolved (None).
    """
    now = now or _now()
    clean: list[tuple[str, str]] = []
    for t, v in refs:
        nv = normalize_id(t, v)
        if nv:
            clean.append((t, nv))
    clean = list(dict.fromkeys(clean))
    stored = fdb.get_security_identifiers(clean)
    days = float(cfg["mapping_refresh_days"])
    result: dict[tuple[str, str], str | None] = {}
    todo: list[tuple[str, str]] = []
    for ref in clean:
        row = stored.get(ref)
        if row is not None and _fresh(row, now, days):
            result[ref] = row["ticker"] if row["status"] == "mapped" else None
        else:
            result[ref] = row["ticker"] if row is not None and row["status"] == "mapped" else None   # stale: usable until replaced
            todo.append(ref)
    if not todo or not float(cfg["openfigi_enabled"]):
        return result
    todo = todo[:max(0, int(float(cfg["openfigi_max_ids_per_refresh"])))]
    client = client or OpenFigiClient(timeout=float(cfg.get("nport_http_timeout_seconds", 30.0)))
    size = max(1, min(int(float(cfg["openfigi_batch_size"])), client.max_batch))
    for i in range(0, len(todo), size):
        batch = todo[i:i + size]
        try:
            answers = client.map([{"idType": _ID_TYPE[t], "idValue": v} for t, v in batch])
        except MappingError:
            break                                                  # stop for this run; nothing from this batch is cached
        rows: list[dict] = []
        for (t, v), ans in zip(batch, answers):
            if not isinstance(ans, dict) or "error" in ans:
                continue                                           # a per-job error is transient: not cached
            listing = pick_listing(ans.get("data") or [])
            stamp = now.isoformat(timespec="seconds")
            if listing is None:
                if "data" in ans or "warning" in ans:
                    rows.append({"id_type": t, "id_value": v, "status": "not_found", "resolved_at": stamp})
                    result[(t, v)] = None
                continue
            ticker = listing["ticker"].strip().upper()
            rows.append({"id_type": t, "id_value": v, "status": "mapped", "ticker": ticker, "name": listing.get("name"),
                         "exch_code": listing.get("exchCode"), "security_type": listing.get("securityType"),
                         "figi": listing.get("figi"), "resolved_at": stamp})
            result[(t, v)] = ticker
        fdb.save_security_identifiers(rows)                        # own short transaction, no network inside it
    return result


def _ref_for(h) -> list[tuple[str, str]]:
    """Identifier refs to try for a holding, CUSIP first."""
    out = []
    for t, v in (("CUSIP", getattr(h, "cusip", None)), ("ISIN", getattr(h, "isin", None))):
        nv = normalize_id(t, v)
        if nv:
            out.append((t, nv))
    return out


def map_fund_holdings(data: FundData, cfg: dict, *, client: OpenFigiClient | None = None, now: datetime | None = None) -> FundData:
    """
    Fill `symbol` on the holdings that have a CUSIP / ISIN but no ticker, and set mapped_coverage (the weight that now has a ticker, vs
    holdings_coverage, the weight the holdings explain). Holdings that cannot be mapped keep symbol None and stay identified by their
    CUSIP / ISIN. Largest holdings are looked up first. A CUSIP that is not found is retried as its ISIN. Mutates and returns `data`.
    """
    need = sorted((h for h in data.holdings if not h.symbol and _ref_for(h)), key=lambda h: -h.weight)
    if need:
        resolved = resolve_identifiers([r for h in need for r in _ref_for(h)[:1]], cfg, client=client, now=now)
        # a CUSIP OpenFIGI answered "not found" for is retried as the holding's ISIN (only a stored miss: a CUSIP not looked up at all
        # because of the per-run cap, or failing transiently, is not a miss)
        first = [_ref_for(h)[0] for h in need if len(_ref_for(h)) > 1]
        stored = fdb.get_security_identifiers(first) if first else {}
        second = [_ref_for(h)[1] for h in need
                  if len(_ref_for(h)) > 1 and stored.get(_ref_for(h)[0], {}).get("status") == "not_found"]
        if second:
            resolved.update(resolve_identifiers(second, cfg, client=client, now=now))
        mapped: dict[int, str] = {}
        for h in need:
            for ref in _ref_for(h):
                tk = resolved.get(ref)
                if tk:
                    mapped[id(h)] = tk
                    break
        data.holdings = [replace(h, symbol=mapped[id(h)]) if id(h) in mapped else h for h in data.holdings]
    data.mapped_coverage = round(min(1.0, sum(h.weight for h in data.holdings if h.symbol)), 12)
    if any(not h.symbol and _ref_for(h) for h in data.holdings):
        data.reason_codes = list(dict.fromkeys([*data.reason_codes, RC_HOLDINGS_UNMAPPED]))
    return data
