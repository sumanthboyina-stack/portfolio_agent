"""
Risk + Technical specialists — daily portfolio flagging (Approach B).

Runs alongside APEX for portfolio holdings only, writing to the standalone
`risk_flags` table rather than the `predictions` table / weight engine — no
schema change to predictions, no re-weighting, no disruption to the
composite-score history the Validation/Track Record tooling analyzes.
`flag=1` rows feed the dashboard's Attention Queue directly (see
web/app.py::_build_queue).

Risk is per-owner, Technical is shared (see tools.risk_flags_db's module
docstring for why): the Risk specialist's own tools only see one ticker at a
time and can't compute correlation/sector-aggregate/beta/issuer-concentration
across a portfolio — portfolio_risk.py computes that once PER OWNER (pure
math, no LLM) and this module folds it into the Risk specialist's query text,
so two owners holding the same ticker get their own numbers, not one blended
across everyone's combined book. Technical output (SMA/RSI/MACD/momentum) is
pure market data, computed once per ticker regardless of who holds it.

Technical: no LLM. The indicator math (SMA/RSI/MACD/Bollinger/ATR) is computed
and stored by tools.technical_features, and the flag/score/summary written to
risk_flags is derived from those numbers by plain rules (_rule_based_technical).
The row keeps the same fields the old LLM narrative had (primary_trend, momentum,
macd_signal, technical_score, summary, ...) so readers are unaffected; options-
derived fields (implied_volatility, put_call_ratio) and support/resistance are
null. The on-demand Technical specialist (chat) still exists for questions.

Risk cadence (event-driven, like research): a holding's risk check re-runs only
when something changed — a big 1-day price move, a severity-3 event (news /
8-K / rating), a new filing, or a change to the owner's holdings — otherwise
weekly (tools.risk_flags_db.risk_refresh_reason). The portfolio-wide numbers
(weight, sector/issuer concentration, correlation, beta, volatility) are
computed without AI and merged into every saved row; the LLM only adds the
score, flags and narrative, 10 tickers per call (same _BATCH_SIZE as research
and fundamentals).
"""

from __future__ import annotations

import json
from datetime import date

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.tools.risk_flags_db import HOLDINGS_SIG_KEY


def _owners_and_their_tickers() -> dict[str, list[str]]:
    """
    owner -> sorted unique tickers currently held in that owner's portfolio,
    for every ENABLED auth_users owner (never every row in `portfolios` —
    a portfolio can outlive the identity that created it, e.g. after a
    rename/re-invite, and computing risk for an owner no login can reach
    would just burn LLM calls on unreachable data). Owners with no holdings
    are omitted. System-wide batch use only — never a per-request read.
    """
    from portfolio_agent.tools import auth_users_db, holdings_db as repo

    out: dict[str, list[str]] = {}
    for u in auth_users_db.list_users():
        if not u.enabled:
            continue
        portfolio = repo.get_portfolio_by_owner(u.owner)
        if portfolio is None:
            continue
        tickers = sorted({h["ticker"] for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"]) if h.get("ticker")})
        if tickers:
            out[u.owner] = tickers
    return out


def _holdings_signature(holdings: list[dict]) -> str:
    """Stable fingerprint of WHAT an owner holds (tickers + share counts) — deliberately
    not prices/values, which drift daily and would re-trigger every run."""
    import hashlib
    parts = sorted(
        f"{str(h.get('ticker', '')).upper()}:{round(float(h.get('shares') or 0), 4)}"
        for h in holdings if h.get("ticker")
    )
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _price_stats(tickers: list[str]) -> dict[str, dict]:
    """{TICKER: {move_pct, daily_vol_pct}} from the shared cached daily closes — pure math,
    no LLM. Missing/short history simply leaves the ticker out (never triggers)."""
    from portfolio_agent.tools.portfolio_risk import _fetch_closes
    out: dict[str, dict] = {}
    try:
        closes = _fetch_closes(sorted(set(tickers)))
    except Exception:
        return out
    for t, ser in closes.items():
        try:
            ser = ser.dropna()
            if len(ser) < 3:
                continue
            rets = ser.pct_change().dropna()
            out[t.upper()] = {
                "move_pct": float((ser.iloc[-1] / ser.iloc[-2] - 1) * 100),
                "daily_vol_pct": float(rets.std() * 100),
                "last_close": float(ser.iloc[-1]),
            }
        except Exception:
            continue
    return out


def _events_since(since_iso: str) -> dict[str, str]:
    """{TICKER: event_type} for events at/after *since_iso* with severity >= the risk trigger."""
    from portfolio_agent.events.db import list_events_since
    from portfolio_agent.tools.risk_flags_db import EVENT_MIN_SEVERITY
    try:
        rows = list_events_since(since_iso, min_severity=EVENT_MIN_SEVERITY)
    except Exception:
        return {}
    return {str(r["ticker"]).upper(): r.get("event_type") or "event" for r in rows}


def _latest_filing_dates(tickers: list[str]) -> dict[str, str]:
    """{TICKER: newest filing_date} from the fundamentals table (best effort)."""
    from portfolio_agent.tools.db import db_conn
    out: dict[str, str] = {}
    try:
        with db_conn() as c:
            for t in tickers:
                row = c.execute(
                    "SELECT MAX(filing_date) AS d FROM fundamentals WHERE ticker = ?", (t.upper(),)
                ).fetchone()
                if row and row["d"]:
                    out[t.upper()] = row["d"]
    except Exception:
        pass
    return out


def _precomputed_risk_numbers(holdings: list[dict], ticker: str, stats: dict, rctx: dict | None) -> dict:
    """The deterministic (no-AI) risk fields for one holding — merged into the saved row
    verbatim; the LLM never gets to restate or alter them."""
    def _value(h: dict) -> float:
        v = h.get("current_value")
        if v is None:
            px = (stats.get(str(h.get("ticker", "")).upper()) or {}).get("last_close") or h.get("current_price") or 0
            v = float(h.get("shares") or 0) * float(px)
        return float(v or 0)

    total = sum(_value(h) for h in holdings) or 0.0
    t = ticker.upper()
    mine = [h for h in holdings if str(h.get("ticker", "")).upper() == t]
    weight = sum(_value(h) for h in mine)
    sector = next((h.get("sector") for h in mine if h.get("sector")), None) or "Unknown"
    sector_val = sum(_value(h) for h in holdings if (h.get("sector") or "Unknown") == sector)
    pct = (lambda v: round(v / total * 100, 2)) if total else (lambda v: None)
    vol = (stats.get(t) or {}).get("daily_vol_pct")
    r = rctx or {}
    return {
        "ticker": t,
        "current_portfolio_weight_pct": pct(weight),
        "sector": sector,
        "sector_exposure_pct": pct(sector_val),
        "daily_volatility_pct": round(vol, 2) if vol is not None else None,
        "correlation_with_portfolio": r.get("correlation_with_portfolio"),
        "most_correlated_peer": r.get("most_correlated_peer"),
        "sector_concentration_pct": r.get("sector_concentration_pct", pct(sector_val)),
        "beta_vs_spy": r.get("beta_vs_spy"),
        "issuer_concentration_pct": r.get("issuer_concentration_pct"),
        "issuer_peers": r.get("issuer_peers") or [],
    }


def _format_risk_block(numbers: dict, reason: str) -> str:
    fields = {k: v for k, v in numbers.items() if k != "ticker"}
    return f"=== {numbers['ticker']} ===\nWhy re-checked: {reason}\nPre-computed: {json.dumps(fields)}"


async def _call_risk_batch(prompt: str, chain: list, model_idx: int) -> tuple[list[dict], int]:
    """One batched risk LLM call with model failover (same chain as research/fundamentals).
    Returns (analyses, model_idx_used). Raises RuntimeError when every model is exhausted."""
    import litellm as _litellm
    from portfolio_agent._models import GEMINI_COUNTER
    from portfolio_agent.pipeline.failover import _is_failover_error
    from portfolio_agent.pipeline.json_parsing import _extract_json_array

    last_exc: BaseException | None = None
    while model_idx < len(chain):
        model_id, provider, _label = chain[model_idx]
        try:
            resp = await _litellm.acompletion(
                model=model_id, messages=[{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=8000,
            )
            analyses = _extract_json_array(resp.choices[0].message.content or "")
            if not analyses:
                model_idx += 1
                continue
            if provider == "google":
                GEMINI_COUNTER.record(model_id)
            return analyses, model_idx
        except Exception as exc:
            last_exc = exc
            if _is_failover_error(exc):
                model_idx += 1
                continue
            raise
    raise RuntimeError(f"all risk models exhausted ({type(last_exc).__name__ if last_exc else 'no JSON'})")


async def _run_daily_risk_technical(portfolio_tickers: list[str], tracker=None) -> None:
    log = _get_logger("risk_technical")
    from portfolio_agent._models import FAILOVER_CHAINS
    from portfolio_agent.tools.risk_flags_db import needs_technical_refresh, risk_refresh_reason
    from portfolio_agent.tools.portfolio_risk import compute_portfolio_risk_context
    from portfolio_agent.pipeline.prompt_templates import _BATCH_SIZE, _BATCH_RISK_PROMPT
    from portfolio_agent.tools import holdings_db as repo

    if not portfolio_tickers:
        log.info("  No portfolio tickers — skipping Risk/Technical phase.", event_type="info")
        if tracker:
            tracker.start_phase("risk_technical", total=0)
            tracker.finish_phase("risk_technical", 0, 0, note="no portfolio tickers")
        return

    today = date.today().isoformat()

    # Technical: shared, ticker-only (union across every owner — same list
    # portfolio_tickers already is).
    technical_to_run = [t for t in dict.fromkeys(portfolio_tickers) if needs_technical_refresh(t, today)]

    # Risk: per owner, only that owner's own holdings — and only the ones
    # whose risk check is due (weekly, or something changed).
    owner_tickers = _owners_and_their_tickers()
    all_held = sorted({t for ts in owner_tickers.values() for t in ts})
    stats = _price_stats(all_held) if all_held else {}
    filings = _latest_filing_dates(all_held) if all_held else {}

    owner_holdings: dict[str, list[dict]] = {}
    risk_to_run: dict[str, list[tuple[str, str]]] = {}   # owner -> [(ticker, reason)]
    for owner, tickers in owner_tickers.items():
        try:
            portfolio = repo.get_portfolio_by_owner(owner)
            owner_holdings[owner] = (
                [h.to_dict() for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"])] if portfolio else []
            )
        except Exception as exc:
            log.warning(f"  [warn] Could not load holdings for {owner!r}: {exc}", event_type="warning")
            continue
        sig = _holdings_signature(owner_holdings[owner])
        for t in tickers:
            reason = risk_refresh_reason(
                t, today, owner,
                holdings_sig=sig,
                price_move_pct=(stats.get(t) or {}).get("move_pct"),
                event_tickers_since=_events_since,
                new_filing_date=filings.get(t),
            )
            if reason:
                risk_to_run.setdefault(owner, []).append((t, reason))

    n_risk = sum(len(v) for v in risk_to_run.values())
    n_held = sum(len(v) for v in owner_tickers.values())

    if not technical_to_run and not n_risk:
        log.info(
            f"  Risk: {n_held} holding(s) current (no change, under a week old); technical already flagged today.",
            event_type="info",
        )
        if tracker:
            tracker.start_phase("risk_technical", total=0)
            tracker.finish_phase("risk_technical", 0, 0, note="nothing changed")
        return

    risk_batches = sum((len(v) + _BATCH_SIZE - 1) // _BATCH_SIZE for v in risk_to_run.values())
    total_calls = len(technical_to_run) + risk_batches
    if tracker:
        tracker.start_phase("risk_technical", total=total_calls)

    completed = 0
    failed: list[str] = []
    chain = FAILOVER_CHAINS.get("flash", [])
    model_idx = 0
    log.info(
        f"  Risk: {n_risk}/{n_held} holding(s) due → {risk_batches} batched call(s) "
        f"({_BATCH_SIZE}/call). Reasons: "
        + ", ".join(f"{o!r}/{t}={r}" for o, v in risk_to_run.items() for t, r in v[:6]),
        event_type="info",
    )

    # ── Risk pass — batched, grouped by owner so risk_ctx is computed once per owner ──
    risk_call_i = 0
    for owner, due in risk_to_run.items():
        holdings = owner_holdings[owner]
        sig = _holdings_signature(holdings)
        try:
            risk_ctx = compute_portfolio_risk_context(holdings)
        except Exception as exc:
            log.warning(
                f"  [warn] Portfolio risk context failed for {owner!r} ({exc}) — numbers will be partial.",
                event_type="warning",
            )
            risk_ctx = {}

        for k in range(0, len(due), _BATCH_SIZE):
            chunk = due[k: k + _BATCH_SIZE]
            risk_call_i += 1
            numbers = {t: _precomputed_risk_numbers(holdings, t, stats, risk_ctx.get(t.upper())) for t, _ in chunk}
            prompt = _BATCH_RISK_PROMPT.format(
                n=len(chunk),
                data_blocks="\n\n".join(_format_risk_block(numbers[t], r) for t, r in chunk),
            )
            analyses: list[dict] = []
            try:
                analyses, model_idx = await _call_risk_batch(prompt, chain, model_idx)
            except Exception as exc:
                log.warning(f"  [warn] Risk batch {risk_call_i} failed for {owner!r}: {exc}", event_type="warning")
                failed.extend(f"{owner}:{t}:risk" for t, _ in chunk)

            by_ticker = {
                str(a.get("ticker", "")).upper(): a for a in analyses if isinstance(a, dict) and a.get("ticker")
            }
            label = chain[model_idx][2] if model_idx < len(chain) else ""
            for t, reason in chunk:
                llm = by_ticker.get(t.upper())
                if not llm:
                    if analyses:
                        failed.append(f"{owner}:{t}:risk")
                    continue
                # Deterministic numbers win; the LLM supplies score/flags/narrative only.
                data = {**llm, **numbers[t], HOLDINGS_SIG_KEY: sig, "refresh_reason": reason}
                _save_risk_flag(t, today, data, label, owner)
                completed += 1
                log.info(f"  [risk] {owner!r}/{t} — ✓ ({reason})", event_type="db_write", ticker=t)
            if tracker:
                tracker.tick("risk_technical", completed, total_calls, ticker=chunk[0][0],
                            note=f"risk batch {risk_call_i}/{risk_batches}")

    # ── Technical pass — once per ticker, shared across every owner; no LLM ──
    from portfolio_agent.tools.technical_features import fetch_and_store_technical_features
    for i, ticker in enumerate(technical_to_run, 1):
        tech_data: dict = {}
        try:
            row = fetch_and_store_technical_features(ticker)
            tech_data = _rule_based_technical(ticker, (row or {}).get("features") or {})
        except Exception as exc:
            log.warning(f"  [warn] Technical features failed for {ticker}: {exc}", event_type="warning")
        if tech_data:
            _save_technical_flag(ticker, today, tech_data, "rules/technical-features")
            completed += 1
        else:
            failed.append(f"{ticker}:technical")
        if tracker:
            tracker.tick("risk_technical", completed, total_calls, ticker=ticker,
                        note=f"technical {i}/{len(technical_to_run)}")
        log.info(
            f"  [technical {i}/{len(technical_to_run)}] {ticker} — "
            f"technical={'✓' if tech_data else '✗'}",
            event_type="db_write", ticker=ticker,
        )

    if tracker:
        tracker.finish_phase("risk_technical", completed, len(failed))
    log.info(
        f"\n  Risk/Technical phase done — {completed} flag(s) saved · {len(failed)} failed",
        event_type="phase_end", saved=completed, failed=len(failed),
    )


def _save_risk_flag(ticker: str, as_of_date: str, data: dict, model_used: str, owner: str) -> None:
    from portfolio_agent.tools.risk_flags_db import upsert_risk_flag

    score = data.get("portfolio_risk_score")
    flag = bool(
        data.get("concentration_flag")
        or (isinstance(score, (int, float)) and score >= 8)
        or (data.get("sector_concentration_pct") or 0) > 25
        or (data.get("issuer_concentration_pct") or 0) > 15
        or abs(data.get("beta_vs_spy") or 0) > 1.5
    )
    upsert_risk_flag(
        ticker=ticker, as_of_date=as_of_date, source="risk",
        flag=flag, score=score, summary=data.get("summary", ""),
        raw_json=json.dumps(data), model_used=model_used, owner=owner,
    )


def _save_technical_flag(ticker: str, as_of_date: str, data: dict, model_used: str) -> None:
    from portfolio_agent.tools.risk_flags_db import upsert_risk_flag

    score = data.get("technical_score")
    flag = bool(
        data.get("momentum") == "BEARISH"
        or (isinstance(score, (int, float)) and score <= 3)
    )
    upsert_risk_flag(
        ticker=ticker, as_of_date=as_of_date, source="technical",
        flag=flag, score=score, summary=data.get("summary", ""),
        raw_json=json.dumps(data), model_used=model_used,
    )


def _rule_based_technical(ticker: str, f: dict) -> dict:
    """
    Deterministic replacement for the Technical LLM narrative: trend, momentum, score
    and a templated summary from technical_features' stored numbers. {} when there is
    no usable price/SMA data (the caller counts that as a failure, as before).
    """
    def num(k):
        v = f.get(k)
        return v if isinstance(v, (int, float)) else None

    price, sma20, sma50, sma200 = num("current_price"), num("sma_20"), num("sma_50"), num("sma_200")
    rsi, hist = num("rsi_14"), num("macd_histogram")
    if price is None or (sma50 is None and sma20 is None):
        return {}

    slow = sma200 if sma200 is not None else sma50
    mid = sma50 if sma50 is not None else sma20
    if slow is not None and mid is not None and price > mid > slow:
        trend = "UPTREND"
    elif slow is not None and mid is not None and price < mid < slow:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    votes = 0
    if hist is not None:
        votes += 1 if hist > 0 else -1 if hist < 0 else 0
    if rsi is not None:
        votes += 1 if rsi > 50 else -1 if rsi < 50 else 0
    if sma20 is not None:
        votes += 1 if price > sma20 else -1 if price < sma20 else 0
    momentum = "BULLISH" if votes >= 2 else "BEARISH" if votes <= -2 else "NEUTRAL"
    macd_signal = "NEUTRAL" if hist is None or hist == 0 else "BULLISH" if hist > 0 else "BEARISH"

    score = 5 + {"UPTREND": 2, "DOWNTREND": -2, "SIDEWAYS": 0}[trend]
    score += {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}[momentum]
    score += {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}[macd_signal]
    if rsi is not None and rsi > 70:
        score -= 1      # overbought: stretched, less attractive entry
    score = max(1, min(10, score))

    vs200 = (price / sma200 - 1) * 100 if sma200 else None
    rsi_note = ("overbought" if rsi is not None and rsi > 70
                else "oversold" if rsi is not None and rsi < 30 else None)
    summary = (
        f"{ticker.upper()} is in a {trend.lower()} with {momentum.lower()} momentum "
        f"(MACD {macd_signal.lower()}"
        + (f", RSI {rsi:.0f}" if rsi is not None else "") + "). "
        + (f"Price is {num('price_vs_sma50_pct'):+.1f}% vs its 50-day average" if num("price_vs_sma50_pct") is not None else "")
        + (f" and {vs200:+.1f}% vs its 200-day" if vs200 is not None else "")
        + (f"; RSI reads {rsi_note}." if rsi_note else ".")
    ).replace(" .", ".")
    return {
        "ticker": ticker.upper(), "current_price": price, "primary_trend": trend,
        "momentum": momentum, "rsi_14": rsi, "macd_signal": macd_signal,
        "price_vs_sma50_pct": num("price_vs_sma50_pct"),
        "price_vs_sma200_pct": round(vs200, 2) if vs200 is not None else None,
        "implied_volatility": None, "put_call_ratio": None,
        "support_level": None, "resistance_level": None,
        "technical_score": score, "summary": summary, "source": "rules",
    }
