"""
Fund name -> ticker SUGGESTIONS for statement rows that have a name but no ticker.

A 401(k) statement lists many public mutual funds without their tickers. This module proposes candidates; it NEVER decides: a suggestion is shown in
the import preview and applies only if the person picks it. Nothing here writes anything.

  looks_like_public_fund(name, hint)   a heuristic gate: the row is a mutual fund / target-date fund with a retail fund-family style name, and is not
                                       a collective trust, stable value fund, separate account or company stock (those have no ticker to find).
  normalize_fund_name(name)            lower-case tokens without share-class words ("Admiral", "Class A", "Investor", "Institutional", "Fund" ...).
  suggest_tickers(rows, searcher)      {fund_key: [{symbol, name, score}]}: up to 3 candidates per row whose name is a close match (token overlap
                                       >= MIN_SCORE) and whose type is a mutual fund / ETF. Rows that do not look like public funds get nothing.
  YFinanceNameSearch                   the default searcher (yfinance's search; a network call, made only when the person asks for the lookup, never
                                       inside a database transaction). Any searcher with .search(query) -> list[dict] works, so tests inject a fake.
"""
from __future__ import annotations

import re

MIN_SCORE = 0.6
MAX_SUGGESTIONS = 3

_CLASS_WORDS = {
    "fund", "funds", "trust", "inc", "class", "cl", "shares", "share", "admiral", "investor", "institutional", "inst", "retail", "premier", "signature",
    "select", "advisor", "adv", "service", "a", "b", "c", "d", "f", "i", "r", "r1", "r2", "r3", "r4", "r5", "r6", "k", "y", "z", "n", "p", "t", "w", "the",
    "series", "portfolio", "co", "company", "of", "and",
}
_NOT_PUBLIC = re.compile(r"(?i)\b(?:cit|c\.i\.t|collective|commingled|common trust|separate account|stable value|guaranteed|fixed account|capital preservation|"
                         r"company stock|employer stock|esop|group annuity|pooled|managed income|plan cash reserve|investment contract)\b")
_FAMILY_WORDS = re.compile(r"(?i)\b(?:vanguard|fidelity|t\.? ?rowe|american funds|dodge|schwab|pimco|blackrock|ishares|jpmorgan|j\.p\. morgan|columbia|"
                           r"franklin|invesco|oakmark|harbor|metropolitan west|prudential|nuveen|tiaa|lord abbett|mfs|janus|putnam|principal|"
                           r"allspring|wells fargo|state street|northern|goldman|morgan stanley|neuberger|artisan|baron|hartford|american century|"
                           r"parnassus|calvert|dfa|dimensional|federated|eaton vance|delaware|ivy|victory|thrivent|nationwide)\b")
_FUNDISH = re.compile(r"(?i)\b(?:fund|index|portfolio|growth|value|bond|equity|income|retirement|target|stock market|total market|s&p)\b")


def normalize_fund_name(name: str) -> list[str]:
    toks = re.findall(r"[a-z0-9&]+", (name or "").lower().replace("&", " & "))
    return [t for t in toks if t and t not in _CLASS_WORDS]


def looks_like_public_fund(name: str, hint: str | None) -> bool:
    """A mutual fund or target-date fund named like a retail fund, and not a collective trust / stable value / separate account / company stock."""
    if not name or (hint or "") not in ("MUTUAL_FUND", "TARGET_DATE_FUND", "ETF", "OTHER", ""):
        return False
    if _NOT_PUBLIC.search(name):
        return False
    return bool(_FAMILY_WORDS.search(name)) and bool(_FUNDISH.search(name))


def _score(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class YFinanceNameSearch:
    """yfinance's name search. A network call: use it only on request, outside any database transaction. Failures return []."""

    def search(self, query: str) -> list[dict]:
        try:
            import yfinance as yf
            res = yf.Search(query, max_results=8, news_count=0)
            return [q for q in (res.quotes or []) if isinstance(q, dict)]
        except Exception:
            return []


def suggest_for_name(name: str, searcher) -> list[dict]:
    want = normalize_fund_name(name)
    out: list[dict] = []
    seen: set[str] = set()
    try:
        quotes = searcher.search(name) or []
    except Exception:
        return []
    for q in quotes:
        sym = str(q.get("symbol") or "").upper().strip()
        qtype = str(q.get("quoteType") or q.get("typeDisp") or "").upper().replace(" ", "")
        if not sym or sym in seen or qtype not in ("MUTUALFUND", "ETF", "FUND"):
            continue
        cand = str(q.get("longname") or q.get("shortname") or q.get("name") or "")
        score = round(_score(want, normalize_fund_name(cand)), 2)
        if score >= MIN_SCORE:
            seen.add(sym)
            out.append({"symbol": sym, "name": cand, "score": score})
    out.sort(key=lambda c: -c["score"])
    return out[:MAX_SUGGESTIONS]


def suggest_tickers(rows: list[dict], searcher=None) -> dict[str, list[dict]]:
    """{fund_key: candidates} for the ticker-less rows that look like public funds. fund_key is the row's `fund_key` (see holdings_import)."""
    searcher = searcher or YFinanceNameSearch()
    out: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("ticker_less") or not looks_like_public_fund(r.get("fund_name"), r.get("instrument_hint")):
            continue
        cands = suggest_for_name(r["fund_name"], searcher)
        if cands:
            out[r["fund_key"]] = cands
    return out
