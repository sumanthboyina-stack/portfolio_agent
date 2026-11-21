"""
Engine-backed, READ-ONLY chat tools (chat-portfolio-assistant, Phase 2).

Each tool is a thin wrapper over an engine that already exists; the model explains the result and never computes it. Rules every
tool here follows:

  * owner comes only from the signed-in session: `owner` and `ctx` are injected server-side by _execute_chat_tool and appear in NO
    tool schema. No owner (or a ctx that is not that owner's) -> a "no signed-in account" note, never a fallback to all portfolios.
  * nothing is written, placed, approved or queued. Compliance is checked with the read-only clearance evaluation.
  * a ticker the owner may not trade right now (restricted list, blackout, approval pending, unknown) is LABELLED and withheld from
    anything presented as an action.
  * every result carries `source` and `as_of`; third-party / stored free text is wrapped in an UNTRUSTED_DATA block.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal

# ── Result envelope and untrusted text ───────────────────────────────────────

DATA_OPEN = "<<<UNTRUSTED_DATA source={src}>>>"
DATA_CLOSE = "<<<END_UNTRUSTED_DATA>>>"
_MARKERS = ("<<<UNTRUSTED_DATA", "<<<END_UNTRUSTED_DATA>>>")


def data_block(text, source: str = "db") -> str | None:
    """Text that came from outside the app (news, filings, broker research, stored model reasoning) as clearly delimited DATA. The
    system prompt tells the model to ignore any instruction inside it. A marker inside the text is removed so it cannot close the block."""
    if text is None or str(text).strip() == "":
        return None
    clean = str(text)
    for m in _MARKERS:
        clean = clean.replace(m, "")
    return f"{DATA_OPEN.format(src=source)}\n{clean}\n{DATA_CLOSE}"


def _env(source: str, as_of, **data) -> dict:
    return {"source": source, "as_of": as_of, **data}


def _no_account() -> dict:
    return {"source": "none", "as_of": None,
            "note": "No signed-in account, so there is no personal portfolio data to use. Nothing was looked up."}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ── Owner book and compliance ─────────────────────────────────────────────────

def _owner_book(owner: str | None, ctx) -> dict | None:
    """The signed-in owner's own portfolio and account-level holdings, or None when there is no verified owner. Never reads another
    owner's data and never falls back to the combined book."""
    if not owner or ctx is None or getattr(ctx, "actor", None) != owner:
        return None
    from portfolio_agent.tools import holdings_db as repo
    portfolio = repo.get_portfolio_by_owner(owner)
    holdings = ([h.to_dict() for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"])] if portfolio else [])
    total = sum(_num(h.get("current_value")) or ((_num(h.get("shares")) or 0) * (_num(h.get("current_price")) or 0)) for h in holdings)
    return {"portfolio": portfolio, "holdings": holdings, "holdings_value": total}


def _weights(holdings: list[dict]) -> dict[str, float]:
    """{TICKER: % of the book's holdings value}, summed across accounts."""
    vals: dict[str, float] = {}
    for h in holdings:
        v = _num(h.get("current_value"))
        if v is None:
            v = (_num(h.get("shares")) or 0) * (_num(h.get("current_price")) or 0)
        t = str(h.get("ticker") or "").upper()
        if t:
            vals[t] = vals.get(t, 0.0) + v
    total = sum(vals.values())
    return {t: round(100.0 * v / total, 2) for t, v in vals.items()} if total > 0 else {}


def compliance_label(owner: str, ticker: str, *, side: str | None = None) -> dict:
    """
    Read-only: may THIS owner act on this ticker right now? Uses clearance.evaluate_clearance (restricted list, blackout windows,
    approvals) and NEVER the recording variant. The legacy single blackout window on the profile is checked with the pure date
    helper instead of letting evaluate_clearance migrate it into the blackout table (that would be a write).
    {"ticker", "status" (ALLOWED_BY_RULES | BLOCKED | PENDING_APPROVAL | UNKNOWN), "cleared", "reason", "reason_codes"}
    """
    from dataclasses import is_dataclass, replace
    from portfolio_agent import clearance as cl
    from portfolio_agent.domain import UserProfile
    from portfolio_agent.tools.user_profile_db import get_user_profile_by_owner

    t = ticker.upper()
    try:
        profile = get_user_profile_by_owner(owner) or UserProfile()
        legacy = (getattr(profile, "blackout_start", None), getattr(profile, "blackout_end", None))
        safe = replace(profile, blackout_start=None, blackout_end=None) if is_dataclass(profile) else profile
        d = cl.evaluate_clearance(t, safe, side=side, owner_scope=f"user:{owner}")
        status, reason = d.decision, d.reason
        codes = list(d.evidence_refs.get("reason_codes") or [])
        if status in (cl.DECISION_ALLOWED, cl.DECISION_PENDING_APPROVAL) and any(legacy):
            try:
                if cl.is_in_blackout(date.today(), *legacy):             # a blackout outranks "approval pending"
                    status, reason = cl.DECISION_BLOCKED, "inside your profile's blackout window"
                    codes.append(cl.RC_BLACKOUT_ACTIVE)
            except ValueError:
                status, reason = cl.DECISION_UNKNOWN, "your profile's blackout dates could not be read"
        return _label(t, status, reason, codes)
    except Exception as exc:                      # unknown is never "cleared"
        return _label(t, "UNKNOWN", f"could not be checked ({type(exc).__name__})", [])


def _label(ticker: str, status: str, reason, codes: list) -> dict:
    """Three outcomes: ALLOWED_BY_RULES (actionable), PENDING_APPROVAL (shown, but NOT actionable until the owner's own pre-clearance
    approval exists), and BLOCKED / UNKNOWN (withheld: restricted list, blackout, or a check that could not be completed)."""
    return {"ticker": ticker, "status": status, "cleared": status == "ALLOWED_BY_RULES", "needs_approval": status == "PENDING_APPROVAL",
            "withhold": status not in ("ALLOWED_BY_RULES", "PENDING_APPROVAL"), "reason": reason, "reason_codes": codes}


def _compliance_field(label: dict) -> dict:
    out = {"status": label["status"], "actionable": label["cleared"]}
    if label["needs_approval"]:
        out["note"] = "Not actionable yet: your own rules need a pre-clearance approval for this ticker before any trade."
    return out


def _withheld(label: dict) -> dict:
    return {"ticker": label["ticker"], "status": label["status"], "reason": label["reason"],
            "note": "Labelled, not suggested: no new trade in this ticker is currently permitted by your own rules."}


# ── Tool schemas (no owner / portfolio / account id anywhere) ────────────────

def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": properties or {}}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


ENGINE_TOOL_SCHEMAS: list[dict] = [
    _tool("get_personal_opportunities",
          "Today's ranked opportunities from the Opportunity Engine, judged against the USER'S OWN holdings: for each candidate, whether they "
          "already hold it, its weight, its correlation with their holdings, sector concentration before/after and beta. Tickers the user's "
          "own rules do not currently permit are listed separately and must not be suggested. Use for 'what looks good for me', 'new ideas "
          "for my portfolio'.",
          {"top_n": {"type": "integer", "description": "How many to return (default 8, max 15)."}}),
    _tool("recommend_cash_allocation",
          "Where a sum of new cash could go, from the rebalance engine run on the USER'S OWN holdings and limits (whole shares, every mandate "
          "constraint checked, their restricted list excluded). Returns the allocation, any trims, cash left over, the constraint results and "
          "the before/after risk. Use for 'I have $5,000, where should it go?'. Informational, not advice.",
          {"cash_amount": {"type": "number", "description": "Dollars to allocate (> 0)."},
           "top_n": {"type": "integer", "description": "Max candidates to consider (default 5)."}}, ["cash_amount"]),
    _tool("simulate_hypothetical_trades",
          "Run what-if trades through the scenario engine on the USER'S OWN book and show the resulting positions, cash, risk and every "
          "constraint that passes or fails. The result is HYPOTHETICAL, not a forecast. Each trade: ticker, side BUY or SELL, and exactly one "
          "of fraction (SELL only, 0-1 of the shares held), quantity (shares) or amount (dollars). Never include account ids. Use for "
          "'what if I sell half my AAPL and buy VOO?'.",
          {"trades": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
              "ticker": {"type": "string"}, "side": {"type": "string", "enum": ["BUY", "SELL"]},
              "fraction": {"type": "number"}, "quantity": {"type": "number"}, "amount": {"type": "number"}},
              "required": ["ticker", "side"]}}}, ["trades"]),
    _tool("get_fund_lookthrough",
          "What the user REALLY owns once their funds/ETFs are looked through: top issuers (direct + via funds), overlap between positions, "
          "sector mix and how much of the funds' holdings is known. With a ticker, the direct and via-fund exposure to that company. Use for "
          "'what do I really own', 'how much NVDA do I own including funds'.",
          {"ticker": {"type": "string", "description": "Optional: one company's direct + indirect exposure."}}),
    _tool("get_valuation",
          "Valuation for a stock: the stored DCF scenarios (bear/base/bull, probabilities, expected value, margin of safety, label), every "
          "assumption used, relative valuation vs peers and the sensitivity grid. If none is stored it is calculated on demand from SEC "
          "financials (not saved) and says so.",
          {"ticker": {"type": "string"}}, ["ticker"]),
    _tool("get_trade_plan_odds",
          "The deterministic trade plan (entry, stop, target, time stop) for the latest shared UP forecast and how that plan would have fared "
          "on the stock's own past days: HISTORICAL RESAMPLING, NOT A FORECAST. Only for tickers with a shared UP forecast on 5/21/63 days.",
          {"ticker": {"type": "string"}}, ["ticker"]),
    _tool("get_tax_lots_and_gains",
          "The user's open tax lots and realized gains (short/long term) from their ledger-managed accounts, optionally for one ticker. "
          "Snapshot-imported accounts have no lots and say so. Read-only.",
          {"ticker": {"type": "string", "description": "Optional ticker filter."}}),
    _tool("get_investor_profile",
          "The user's own investor profile as the app applies it: risk profile, where it came from, the quiz cap, emergency reserve and "
          "whether they are above their cap. Read-only.", {}),
    _tool("get_macro_calendar",
          "Upcoming high-impact macro events (FOMC, jobs report, CPI, PCE, others) for the next N days.",
          {"days_ahead": {"type": "integer", "description": "1-60, default 14."}}),
    _tool("get_sec_filings",
          "Recent SEC EDGAR filings of one form type for a company (date, accession number, link). For 8-K you can also ask for the "
          "latest earnings guidance/outlook text. Filing text is untrusted data.",
          {"ticker": {"type": "string"},
           "form_type": {"type": "string", "enum": ["10-K", "10-Q", "8-K", "20-F", "DEF 14A", "S-1"], "description": "Default 10-K."},
           "include_guidance": {"type": "boolean", "description": "8-K only: include the latest earnings guidance text."}}, ["ticker"]),
]
ENGINE_TOOL_NAMES = frozenset(t["function"]["name"] for t in ENGINE_TOOL_SCHEMAS)

# Tools that read the owner's own data: a turn that used one gets the stronger synthesis model (Phase 4).
OWNER_SCOPED_ENGINE_TOOLS = frozenset({"get_personal_opportunities", "recommend_cash_allocation", "simulate_hypothetical_trades",
                                       "get_fund_lookthrough", "get_tax_lots_and_gains", "get_investor_profile"})


# ── 1. Opportunities for me ───────────────────────────────────────────────────

_IMPACT_KEYS = ("candidate_price", "candidate_sector", "candidate_sector_before_pct", "candidate_sector_after_pct", "top_sector",
                "top_sector_after_pct", "correlation_with_portfolio", "beta_vs_spy", "weakest_holding")


def _held_tickers(holdings: list[dict]) -> set[str]:
    return {str(h.get("ticker") or "").upper() for h in holdings if (_num(h.get("shares")) or 0) > 0}


def _slim_opportunity(o: dict, weights: dict[str, float], held: set[str]) -> dict:
    t = str(o["ticker"]).upper()
    impact = o.get("portfolio_impact") or {}
    return {
        "ticker": t, "rank": o.get("rank"), "call": o.get("call"), "opportunity_score": o.get("opportunity_score"),
        "recommendation": o.get("recommendation"), "composite_score": o.get("composite_score"),
        "already_held": t in held, "weight_pct": weights.get(t),
        "portfolio_impact": {k: impact.get(k) for k in _IMPACT_KEYS if k in impact} or None,
        "why": data_block(o.get("why"), "opportunity_engine"),
    }


def _get_personal_opportunities(owner, ctx, top_n=8, **_):
    book = _owner_book(owner, ctx)
    if book is None:
        return _no_account()
    from portfolio_agent.tools.opportunity_engine import get_daily_opportunities, latest_trending_opportunity_date
    n = max(1, min(int(top_n or 8), 15))
    items = get_daily_opportunities(top_n=15, holdings=book["holdings"])
    weights, held = _weights(book["holdings"]), _held_tickers(book["holdings"])
    shown, withheld = [], []
    for o in items:
        label = compliance_label(owner, o["ticker"], side="BUY")
        if label["withhold"]:
            withheld.append(_withheld(label))
        elif len(shown) < n:
            shown.append({**_slim_opportunity(o, weights, held), "compliance": _compliance_field(label)})
    return _env("engine: opportunity_engine (ranked against your own holdings)", latest_trending_opportunity_date(),
                count=len(shown), opportunities=shown, restricted_or_blocked=withheld,
                note=None if items else "The engine has no trending opportunities yet; the morning/intraday batch produces them.")


def rebased_latest_opportunities(owner: str | None, ctx, min_confidence: int = 6, limit: int = 10) -> dict:
    """get_latest_opportunities on the Opportunity Engine ranking, with the keys the tool always had (source, count, as_of,
    opportunities[ticker, recommendation, confidence, composite_score, news_score, research_score, fundamental_score, reasoning,
    created_at]) plus the engine's score/call and, for a signed-in owner, held / compliance labels."""
    from portfolio_agent.tools.opportunity_engine import (
        _latest_trending_opportunity_predictions, get_daily_opportunities, latest_trending_opportunity_date,
    )
    book = _owner_book(owner, ctx)
    ranked = get_daily_opportunities(top_n=max(int(limit or 10), 1) * 2, holdings=(book["holdings"] if book else None))
    raw = {str(r["ticker"]).upper(): r for r in _latest_trending_opportunity_predictions()}
    weights = _weights(book["holdings"]) if book else {}
    held = _held_tickers(book["holdings"]) if book else set()
    out, withheld = [], []
    for o in ranked:
        t = str(o["ticker"]).upper()
        r = raw.get(t, {})
        if (r.get("confidence") or 0) < (min_confidence or 0) and len(ranked) > 3:
            continue
        label = compliance_label(owner, t, side="BUY") if owner else None
        if label and label["withhold"]:
            withheld.append(_withheld(label))
            continue
        out.append({
            "ticker": t, "recommendation": r.get("recommendation") or o.get("recommendation"), "confidence": r.get("confidence"),
            "composite_score": r.get("composite_score") if r.get("composite_score") is not None else o.get("composite_score"),
            "news_score": r.get("news_score"), "research_score": r.get("research_score"), "fundamental_score": r.get("fundamental_score"),
            "reasoning": data_block(r.get("reasoning"), "stored prediction"), "created_at": r.get("created_at"),
            "opportunity_score": o.get("opportunity_score"), "call": o.get("call"), "why": data_block(o.get("why"), "opportunity_engine"),
            **({"already_held": t in held, "weight_pct": weights.get(t)} if book else {}),
            **({"compliance": _compliance_field(label)} if label else {}),
        })
        if len(out) >= int(limit or 10):
            break
    if not out and not withheld:
        return {"source": "engine: opportunity_engine", "count": 0, "as_of": latest_trending_opportunity_date(),
                "note": "No trending opportunity predictions found yet. The pipeline discovers trending tickers during intraday and "
                        "morning batch runs. Check back after the next scheduled run."}
    return {"source": "engine: opportunity_engine" + (" (against your own holdings)" if book else ""), "count": len(out),
            "as_of": latest_trending_opportunity_date(), "opportunities": out,
            **({"restricted_or_blocked": withheld} if withheld else {})}


# ── 2. Cash allocation ────────────────────────────────────────────────────────

def _get_cash_allocation(owner, ctx, cash_amount=None, top_n=5, **_):
    book = _owner_book(owner, ctx)
    if book is None:
        return _no_account()
    amount = _num(cash_amount)
    if amount is None or amount <= 0 or amount > 1e9:
        return {"source": "none", "as_of": None, "note": "cash_amount must be a positive number of dollars."}
    if not book["holdings"]:
        return {"source": "none", "as_of": None, "note": "No holdings or accounts were found for this account, so there is nothing to allocate against."}
    from portfolio_agent.tools.portfolio_optimizer import recommend_allocation
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES
    try:
        res = recommend_allocation(amount, top_n_candidates=max(1, min(int(top_n or 5), 10)), holdings=book["holdings"], scope="all",
                                   owner=owner, opportunity_holdings=book["holdings"], prediction_scopes=SHARED_SCOPES)
    except ValueError as exc:
        return {"source": "engine: portfolio_optimizer", "as_of": None, "note": f"The engine could not run: {exc}"}
    scen = res.get("scenario") or {}
    shown, withheld = [], []
    for a in res.get("allocation", []):
        label = compliance_label(owner, a["ticker"], side="BUY")
        if label["withhold"]:
            withheld.append({**_withheld(label), "amount": a.get("amount")})
        else:
            shown.append({**a, "actionable": label["cleared"], "compliance": _compliance_field(label)})
    trims, withheld_trims = [], []
    for r in res.get("reduce", []):
        label = compliance_label(owner, r["ticker"], side="SELL")
        if label["withhold"]:
            withheld_trims.append({**_withheld(label), "suggested_trim_dollars": r.get("suggested_trim_dollars")})
        else:
            trims.append({**r, "actionable": label["cleared"], "compliance": _compliance_field(label)})
    cleared = shown
    for a in cleared:
        a["why"] = data_block(a.get("why"), "rebalance engine")
    for r in trims:
        if "rationale" in r:
            r["rationale"] = data_block(r.get("rationale"), "rebalance engine")
    allocated = round(sum(_num(a.get("amount")) or 0 for a in cleared), 2)
    actionable_now = round(sum(_num(a.get("amount")) or 0 for a in cleared if a.get("actionable")), 2)
    constraints = scen.get("constraints") or []
    return _env("engine: portfolio_optimizer (scenarios.prepare_inputs + propose_trades + evaluate)",
                (res.get("inputs") or {}).get("price_as_of"),
                cash_amount=amount, allocation=cleared, reduce=trims,
                restricted_or_blocked=withheld + withheld_trims,
                cash_allocated=allocated, cash_actionable_now=actionable_now, cash_not_allocated=round(amount - allocated, 2),
                scenario_status=scen.get("status"), warnings=scen.get("warnings"),
                constraint_failures=[c for c in constraints if c.get("status") != "pass"],
                constraints_passed=sum(1 for c in constraints if c.get("status") == "pass"),
                risk_before_after=res.get("impact"),
                note="Informational output of fixed rules against your own limits; not advice or an instruction to trade.")


# ── 3. Hypothetical trades ────────────────────────────────────────────────────

def _pin_price(inputs, ticker: str):
    """A what-if may name a ticker the book does not hold: pin its latest close so the engine can price it (noted in warnings)."""
    from dataclasses import replace
    from portfolio_agent.scenarios.engine import D
    from portfolio_agent.tools.portfolio_risk import _fetch_closes
    closes = _fetch_closes([ticker])
    ser = closes.get(ticker)
    if ser is None or len(ser) == 0:
        return inputs, False
    px = float(ser.iloc[-1])
    return replace(inputs, prices={**inputs.prices, ticker: D(px)},
                   warnings=tuple(inputs.warnings) + (f"{ticker}: price pinned from its latest close ({px:.2f})",)), True


def _simulate_trades(owner, ctx, trades=None, **_):
    book = _owner_book(owner, ctx)
    if book is None:
        return _no_account()
    if not isinstance(trades, list) or not trades or len(trades) > 10:
        return {"source": "none", "as_of": None, "note": "Give 1 to 10 trades, each with ticker, side and one of fraction / quantity / amount."}
    if not book["holdings"]:
        return {"source": "none", "as_of": None, "note": "No holdings or accounts were found for this account."}
    from portfolio_agent.scenarios.engine import D, Trade, evaluate
    from portfolio_agent.scenarios.prepare import prepare_inputs
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES

    holdings = book["holdings"]
    try:
        inputs, ctx_ = prepare_inputs(scope="all", holdings=holdings, contribution=0, owner=owner,
                                      opportunity_holdings=holdings, prediction_scopes=SHARED_SCOPES)
    except ValueError as exc:
        return {"source": "engine: scenarios", "as_of": None, "note": f"The scenario could not be prepared: {exc}"}

    rows: dict[str, list[dict]] = {}
    for h in holdings:
        if h.get("account_id") is not None and (_num(h.get("shares")) or 0) > 0:
            rows.setdefault(str(h["ticker"]).upper(), []).append(h)

    built: list[Trade] = []
    input_errors: list[str] = []
    sell_proceeds: dict[int, float] = {}
    pending_buys: list[tuple[str, dict]] = []
    seen_tickers: list[str] = []
    for i, t in enumerate(trades, 1):
        tk = str((t or {}).get("ticker") or "").upper().strip()
        side = str((t or {}).get("side") or "").upper()
        given = {k: _num(t.get(k)) for k in ("fraction", "quantity", "amount") if t.get(k) is not None}
        if not tk or side not in ("BUY", "SELL") or len(given) != 1 or any(v is None or v <= 0 for v in given.values()):
            input_errors.append(f"trade {i}: needs a ticker, side BUY or SELL and exactly one positive fraction / quantity / amount")
            continue
        seen_tickers.append(tk)
        if side == "SELL":
            held = rows.get(tk)
            if not held:
                input_errors.append(f"trade {i}: you do not hold {tk}, so there is nothing to sell")
                continue
            total_sh = sum(_num(h["shares"]) for h in held)
            if "fraction" in given and given["fraction"] > 1:
                input_errors.append(f"trade {i}: a fraction cannot exceed 1")
                continue
            for h in held:
                part = _num(h["shares"]) / total_sh
                kw = ({"quantity": D(_num(h["shares"]) * given["fraction"])} if "fraction" in given else
                      {"quantity": D(given["quantity"] * part)} if "quantity" in given else {"amount": D(given["amount"] * part)})
                built.append(Trade(account_id=int(h["account_id"]), ticker=tk, side="SELL", source="user",
                                   why="what-if (hypothetical)", **kw))
                px = _num(inputs.prices.get(tk)) or _num(h.get("current_price")) or 0
                sh = float(kw["quantity"]) if "quantity" in kw else float(kw["amount"]) / px if px else 0
                sell_proceeds[int(h["account_id"])] = sell_proceeds.get(int(h["account_id"]), 0.0) + sh * px
        else:
            if "fraction" in given:
                input_errors.append(f"trade {i}: a BUY takes quantity or amount, not a fraction")
                continue
            pending_buys.append((tk, given))
    funding = (max(sell_proceeds, key=sell_proceeds.get) if sell_proceeds else ctx_.get("funding_account_id"))
    if funding is None and pending_buys:
        input_errors.append("no account could be chosen to fund the buy")
        pending_buys = []
    for tk, given in pending_buys:
        if tk not in inputs.prices:
            inputs, ok = _pin_price(inputs, tk)
            if not ok:
                input_errors.append(f"{tk}: no price could be found, so it cannot be bought in the what-if")
                continue
        kw = {"quantity": D(given["quantity"])} if "quantity" in given else {"amount": D(given["amount"])}
        built.append(Trade(account_id=int(funding), ticker=tk, side="BUY", source="user", why="what-if (hypothetical)", **kw))
    if not built:
        return {"source": "engine: scenarios", "as_of": None, "hypothetical": True, "input_errors": input_errors,
                "note": "No evaluable trades were given."}
    res = evaluate(inputs, built).to_dict()
    constraints = res.get("constraints") or []
    labels = [compliance_label(owner, tk, side=None) for tk in dict.fromkeys(seen_tickers)]
    return _env("engine: scenarios.evaluate (your own holdings, limits and restricted list)", inputs.price_as_of,
                label="HYPOTHETICAL: a what-if on your own book, not a forecast and not advice",
                hypothetical=True, status=res.get("status"), trades=res.get("trades"), rejected=res.get("rejected"),
                constraint_violations=[c for c in constraints if c.get("status") != "pass"],
                constraints_passed=sum(1 for c in constraints if c.get("status") == "pass"),
                warnings=res.get("warnings"), before=res.get("before"), after=res.get("after"), risk=res.get("risk"),
                input_errors=input_errors, input_notes=list(inputs.warnings),
                compliance=[{k: l[k] for k in ("ticker", "status", "reason")} for l in labels],
                compliance_note=("Tickers whose status is not ALLOWED_BY_RULES cannot currently be traded under your own rules; "
                                 "this what-if does not make them actionable." if any(not l["cleared"] for l in labels) else None))


# ── 4. Fund look-through ──────────────────────────────────────────────────────

def _get_fund_lookthrough(owner, ctx, ticker=None, **_):
    book = _owner_book(owner, ctx)
    if book is None:
        return _no_account()
    if book["portfolio"] is None or not book["holdings"]:
        return {"source": "none", "as_of": None, "note": "No holdings found for this account."}
    from portfolio_agent.tools.portfolio_assessment import compute_exposures_for_owner, issuer_key_for
    exp = compute_exposures_for_owner(owner, book["portfolio"])
    out = {"total_value": exp.total_value, "holdings_count": exp.holdings_count}
    if not exp.lookthrough_applicable:
        top = sorted(exp.per_holding_weight_pct.items(), key=lambda kv: -kv[1])[:10]
        return _env("engine: portfolio_assessment (no funds held, so nothing to look through)", exp.built_at, **out,
                    top_holdings_pct=dict(top), lookthrough_applicable=False)
    issuer, via = exp.lookthrough_issuer_pct or {}, exp.lookthrough_issuer_via_pct or {}
    out.update(lookthrough_applicable=True, lookthrough_used=exp.lookthrough_used, coverage_pct=exp.lookthrough_coverage_pct,
               min_coverage_pct=exp.lookthrough_min_coverage_pct, reason_codes=list(exp.lookthrough_reason_codes or ()),
               top_issuers_pct=dict(sorted(issuer.items(), key=lambda kv: -kv[1])[:15]),
               sector_pct=dict(sorted((exp.lookthrough_sector_pct or {}).items(), key=lambda kv: -kv[1])[:12]),
               leveraged_inverse_pct=exp.leveraged_inverse_pct)
    hh = exp.lookthrough.household if exp.lookthrough is not None else None
    if hh is not None:
        out["overlap"] = [{"issuer": o["issuer"], "name": o.get("name"), "pct": o["pct"], "reached_through": o["sources"]}
                          for o in hh.overlap()[:10]]
    if ticker:
        t = str(ticker).upper().strip()
        key = issuer_key_for(t, exp)
        total, indirect = issuer.get(key), via.get(key, 0.0)
        direct = (exp.per_holding_weight_pct or {}).get(t, 0.0)
        via_map = (hh.named_issuers().get(t, {}).get("via") or {}) if hh is not None else {}
        out["ticker_exposure"] = {"ticker": t, "direct_pct": direct, "via_funds_pct": indirect, "total_pct": total,
                                  "reached_through": [{"via": f, "value": round(v, 2)} for f, v in via_map.items()]}
        if total is None and not direct:
            out["ticker_exposure"]["note"] = f"{t} was not found among your direct holdings or the known holdings of your funds."
    return _env("engine: lookthrough.analyze via portfolio_assessment (your own positions and fund data)", exp.built_at, **out,
                note="Percentages are of your total portfolio value. Coverage below the minimum means the look-through is partial.")


# ── 5. Valuation ──────────────────────────────────────────────────────────────

def _compute_valuation_on_demand(ticker: str) -> dict | None:
    """Same steps and engine functions as the daily valuation batch, run for one ticker and NOT saved. None when it cannot be done."""
    from portfolio_agent.macro.fred_client import get_latest_value
    from portfolio_agent.tools.edgar import get_fundamentals_bundle
    from portfolio_agent.tools.edgar_check import _load_cik_map
    from portfolio_agent.tools.universe_db import get_peers
    from portfolio_agent.tools.valuation_engine import relative_valuation, run_dcf_scenarios, sensitivity_grid
    from portfolio_agent.tools.yfinance_tools import get_valuation_multiples

    bundle = get_fundamentals_bundle(ticker, _load_cik_map())
    if bundle.get("error"):
        return None
    revenue, operating_cf, capex = bundle["income"]["revenue"], bundle["cashflow"]["operating_cf"], bundle["cashflow"]["capex"]
    multiples = get_valuation_multiples(ticker)
    rf = get_latest_value("DGS10")
    risk_free = rf.get("value") if "error" not in rf else 4.0
    scenarios = run_dcf_scenarios(revenue, operating_cf, capex, multiples, risk_free)
    if scenarios is None:
        return None
    peers = [get_valuation_multiples(p) for p in get_peers(ticker, limit=5)]
    rel = relative_valuation(multiples, peers, get_valuation_multiples("SPY"))
    grid = sensitivity_grid(revenue, operating_cf, capex, multiples, risk_free)
    return {"scenarios": scenarios, "relative_valuation": rel, "sensitivity_grid": grid, "risk_free_rate_pct": risk_free}


def _get_valuation(owner, ctx, ticker="", **_):
    t = str(ticker or "").upper().strip()
    if not t:
        return {"source": "none", "as_of": None, "note": "A ticker is required."}
    from portfolio_agent.tools.valuation_db import get_stored_valuation
    row = get_stored_valuation(t)
    if row:
        age = None
        try:
            age = (date.today() - date.fromisoformat(str(row["as_of_date"])[:10])).days
        except ValueError:
            pass
        return _env("local DB: stored valuation (daily valuation batch, ~95-day refresh)", row.get("as_of_date"),
                    ticker=t, computed_on_demand=False, age_days=age, stale=(age is not None and age > 95),
                    current_price=row.get("current_price"),
                    intrinsic={"bear": row.get("intrinsic_bear"), "base": row.get("intrinsic_base"), "bull": row.get("intrinsic_bull")},
                    probabilities={"bear": row.get("probability_bear"), "base": row.get("probability_base"), "bull": row.get("probability_bull")},
                    expected_value=row.get("expected_value"), margin_of_safety_pct=row.get("margin_of_safety_pct"),
                    valuation_label=row.get("valuation_label"), assumptions=row.get("assumptions"),
                    relative_valuation=row.get("relative_valuation"), sensitivity_grid=row.get("sensitivity_grid"),
                    model_name=row.get("model_name"))
    try:
        calc = _compute_valuation_on_demand(t)
    except Exception as exc:
        return {"source": "none", "as_of": None, "ticker": t, "note": f"No stored valuation, and one could not be calculated ({type(exc).__name__}). "
                "Funds, cash and tickers without SEC company financials have none."}
    if calc is None:
        return {"source": "none", "as_of": None, "ticker": t,
                "note": "No stored valuation, and one could not be calculated from SEC financials (funds and companies without filings have none)."}
    return _env("engine: valuation_engine, calculated on demand from SEC EDGAR + market data (NOT saved)", date.today().isoformat(),
                ticker=t, computed_on_demand=True, **calc,
                note="Calculated now with the same steps as the daily batch; it is not stored and will differ from a later stored value.")


# ── 6. Trade plan odds ────────────────────────────────────────────────────────

_PLAN_KEYS = ("entry", "stop", "target", "risk_per_share", "reward_risk", "time_stop_date", "atr_14", "stop_rule", "stop_param",
              "target_rule", "target_param", "rules_version", "llm_target")
_SIM_KEYS = ("p_target_first", "p_stop_first", "p_timeout", "p_target_first_zero_drift", "expected_r", "p_close_above_entry",
             "hist_n", "hist_target", "hist_stop", "hist_timeout", "hist_avg_r", "paths", "lookback_days")


def _get_trade_plan_odds(owner, ctx, ticker="", **_):
    t = str(ticker or "").upper().strip()
    if not t:
        return {"source": "none", "as_of": None, "note": "A ticker is required."}
    from portfolio_agent.tools.plan_sim_db import get_flagged_ids, get_sim
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, get_latest_prediction
    from portfolio_agent.tools.trade_plan_db import get_plan
    plans, notes = [], []
    for h in (5, 21, 63):
        p = get_latest_prediction(t, h, scopes=SHARED_SCOPES)
        if p is None or p.id is None:
            notes.append(f"{h}d: no shared forecast on file")
            continue
        if str(p.predicted_direction or "").upper() != "UP":
            notes.append(f"{h}d: the latest shared forecast is {p.predicted_direction or 'not directional'}; a shared plan exists only for UP")
            continue
        plan = get_plan(p.id)
        if not plan or plan.get("plan_status") != "planned":
            notes.append(f"{h}d: no plan ({(plan or {}).get('no_plan_reason') or 'none written'})")
            continue
        sim = get_sim(p.id)
        plans.append({"horizon_days": h, "forecast_as_of": p.prediction_date or (p.created_at or "")[:10], "plan": {k: plan.get(k) for k in _PLAN_KEYS if k in plan},
                      "history_replay": ({k: sim.get(k) for k in _SIM_KEYS if k in sim} if sim and sim.get("sim_status") == "ok" else
                                         {"status": (sim or {}).get("sim_status") or "not run"}),
                      "sim_disagrees": p.id in get_flagged_ids([p.id])})
    return _env("local DB: stored trade plan + plan simulation (tools.trade_plan, tools.plan_simulator)",
                max((x["forecast_as_of"] for x in plans if x.get("forecast_as_of")), default=None),
                ticker=t, plans=plans, notes=notes,
                label="HISTORICAL RESAMPLING OF THIS STOCK'S OWN PAST DAYS, NOT A FORECAST. Stops and targets are set by fixed rules, not by the model.")


# ── 7. Tax lots and gains ─────────────────────────────────────────────────────

def _get_tax_lots_and_gains(owner, ctx, ticker=None, **_):
    if not owner or ctx is None or getattr(ctx, "actor", None) != owner:
        return _no_account()
    from portfolio_agent.services import account_service, ledger_service
    t = str(ticker).upper().strip() if ticker else None
    accounts_out, st_total, lt_total, unknown_proceeds = [], 0.0, 0.0, 0.0
    snapshot_accounts = []
    try:
        accounts = account_service.list_accounts_for(ctx)
    except account_service.NotAuthorized:
        return _env("local DB: ledger", date.today().isoformat(), accounts=[], note="No portfolio was found for this account yet.")
    for acct in accounts:
        aid = acct["account_id"]
        label = acct.get("display_name") or acct.get("account_number") or f"account {aid}"
        if acct.get("holdings_source") != "ledger":
            snapshot_accounts.append(label)
            continue
        lots = [l for l in ledger_service.get_lots(ctx, aid, open_only=True) if not t or l.get("ticker") == t]
        closures = [c for c in ledger_service.get_closures(ctx, aid) if not t or c.get("ticker") == t]
        for c in closures:
            if c.get("basis_unknown"):
                unknown_proceeds += float(c.get("proceeds") or 0)
                continue
            g = float(c.get("realized_gain") or 0)
            if str(c.get("holding_period") or "").upper().startswith("LONG"):
                lt_total += g
            else:
                st_total += g
        accounts_out.append({
            "account": label,
            "open_lots": [{k: l.get(k) for k in ("ticker", "open_date", "quantity", "remaining_quantity", "unit_cost", "cost_basis",
                                                 "basis_unknown") if k in l} for l in lots[:40]],
            "open_lot_count": len(lots),
            "closures": [{k: c.get(k) for k in ("ticker", "quantity", "open_date", "close_date", "proceeds", "cost_basis", "realized_gain",
                                                "holding_period", "basis_unknown") if k in c} for c in closures[-40:]],
            "closure_count": len(closures)})
    return _env("local DB: ledger tax lots and closures (read-only replay of your ledger-managed accounts)", date.today().isoformat(),
                ticker=t, accounts=accounts_out, realized_short_term=round(st_total, 2), realized_long_term=round(lt_total, 2),
                realized_excluded_unknown_basis_proceeds=round(unknown_proceeds, 2),
                snapshot_accounts_without_lots=snapshot_accounts,
                note=("Only ledger-managed accounts have lots; snapshot-imported accounts do not. Sales of shares whose cost was never known "
                      "are left out of the gains. Not tax advice."))


# ── 8. Investor profile ───────────────────────────────────────────────────────

def _get_investor_profile(owner, ctx, **_):
    if not owner or ctx is None or getattr(ctx, "actor", None) != owner:
        return _no_account()
    from portfolio_agent.services.investor_service import get_investor_state
    state = get_investor_state(ctx)
    keep = {k: state.get(k) for k in ("profile", "is_default", "cap_profile", "above_cap", "above_cap_acknowledged",
                                      "emergency_reserve_amount", "emergency_reserve_source", "quiz_version")}
    keep["profile_source"] = state.get("source")          # where the profile came from (the envelope's own `source` is the data source)
    return _env("local DB: your investor profile as the app applies it", date.today().isoformat(), **keep,
                custom_allocation=("set" if state.get("custom_allocation") else None),
                note="Read-only. Change it on the Investor profile page; the chat cannot.")


# ── 9. Macro calendar ─────────────────────────────────────────────────────────

def _get_macro_calendar(owner, ctx, days_ahead=14, **_):
    from portfolio_agent.macro.calendar import get_upcoming_events
    n = max(1, min(int(_num(days_ahead) or 14), 60))
    events = get_upcoming_events(days_ahead=n)
    return _env("macro.calendar (FOMC schedule, computed jobs/CPI/PCE dates, optional Finnhub)", date.today().isoformat(),
                days_ahead=n, count=len(events), events=events,
                note="CPI/PCE dates are estimated from the usual release pattern; at most 10 events are returned.")


# ── 10. SEC filings ───────────────────────────────────────────────────────────

_SEC_MIN_INTERVAL = 0.2          # seconds between EDGAR requests from chat: 5/s, under SEC's 10/s ceiling
_sec_lock = threading.Lock()
_sec_last = [0.0]
_SEC_FORMS = ("10-K", "10-Q", "8-K", "20-F", "DEF 14A", "S-1")


def _sec_throttle() -> None:
    with _sec_lock:
        wait = _SEC_MIN_INTERVAL - (time.monotonic() - _sec_last[0])
        if wait > 0:
            time.sleep(wait)
        _sec_last[0] = time.monotonic()


def _get_sec_filings(owner, ctx, ticker="", form_type="10-K", include_guidance=False, **_):
    import json
    t = str(ticker or "").upper().strip()
    form = str(form_type or "10-K")
    if not t:
        return {"source": "none", "as_of": None, "note": "A ticker is required."}
    if form not in _SEC_FORMS:
        return {"source": "none", "as_of": None, "note": f"form_type must be one of {list(_SEC_FORMS)}."}
    from portfolio_agent.tools import edgar
    _sec_throttle()
    data = json.loads(edgar.get_sec_filings(t, form, limit=5))
    note = None if os.getenv("EDGAR_USER_AGENT") else (
        "EDGAR_USER_AGENT is not set; SEC asks automated clients to identify themselves with a contact address.")
    if "error" in data:
        return {"source": "SEC EDGAR", "as_of": None, "ticker": t, "note": data["error"], **({"config_note": note} if note else {})}
    filings = data.get("filings", [])
    out = _env("SEC EDGAR (submissions API)", filings[0]["date"] if filings else None, ticker=t, form_type=form, filings=filings,
               **({"config_note": note} if note else {}))
    if form == "8-K" and include_guidance:
        _sec_throttle()
        g = edgar.get_earnings_guidance(t)
        out["guidance"] = {"guidance_date": g.get("guidance_date"), "filing_accession": g.get("filing_accession"),
                           "text": data_block(g.get("guidance_text"), "SEC EDGAR 8-K press release"), "error": g.get("error")}
    return out


# ── Dispatcher ────────────────────────────────────────────────────────────────

_RUNNERS = {
    "get_personal_opportunities": _get_personal_opportunities,
    "recommend_cash_allocation": _get_cash_allocation,
    "simulate_hypothetical_trades": _simulate_trades,
    "get_fund_lookthrough": _get_fund_lookthrough,
    "get_valuation": _get_valuation,
    "get_trade_plan_odds": _get_trade_plan_odds,
    "get_tax_lots_and_gains": _get_tax_lots_and_gains,
    "get_investor_profile": _get_investor_profile,
    "get_macro_calendar": _get_macro_calendar,
    "get_sec_filings": _get_sec_filings,
}


def run_engine_tool(name: str, args: dict, owner: str | None, ctx) -> dict | None:
    """The result of an engine tool, or None if *name* is not one of them. owner / ctx are server-side only: anything the model put in
    `args` under those names is dropped."""
    fn = _RUNNERS.get(name)
    if fn is None:
        return None
    clean = {k: v for k, v in (args or {}).items() if k not in ("owner", "ctx", "portfolio_id", "account_id")}
    return fn(owner, ctx, **clean)
