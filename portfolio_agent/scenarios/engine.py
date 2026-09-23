"""
Scenario engine — deterministic evaluation of hypothetical trades against a
frozen snapshot.

Pure: no database, no network, no clock. Everything it needs is in
ScenarioInputs, so the same inputs and trades always give the same result and
a later change to prices, holdings or the profile cannot rewrite an earlier
one (staleness is detected by comparing the pinned `versions`, not by
recomputing).

Arithmetic is Decimal. Products are kept exact internally and quantized only
for presentation (money to 0.01, quantities to 0.0001), so these identities
hold to the cent for every account and for the scope as a whole:

    ending_cash            = starting_cash + external_contribution + sale_proceeds
                             − purchase_cost − fees
    ending_portfolio_value = starting_portfolio_value + external_contribution − fees

Order of operations, per account: resolve each trade to a quantity (rounding
DOWN to the allowed increment), apply it, then validate every constraint on
the rounded quantities. Cash is never pooled across accounts.

Result status
    infeasible     a selected trade or the resulting book violates a constraint
    not_evaluable  a required input (price, sector, starting cash) is missing,
                   so a constraint could not be checked — never silently passed
    feasible       accounting and every supported constraint pass under the
                   stated assumptions
Trades the engine itself proposed (source="proposed") may be rejected without
making the result infeasible; the proposal is whatever survived. Trades the
user selected (source="user") are requirements.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Any, Mapping

ENGINE_VERSION = "scenario-engine/1.0.0"
MONEY = Decimal("0.01")
QTY = Decimal("0.0001")
ZERO = Decimal("0")
ONE = Decimal("1")

STATUS_FEASIBLE = "feasible"
STATUS_INFEASIBLE = "infeasible"
STATUS_NOT_EVALUABLE = "not_evaluable"

PASS, FAIL, NOT_EVALUABLE = "pass", "fail", "not_evaluable"


# ── Decimal helpers ───────────────────────────────────────────────────────────

def D(value: Any) -> Decimal | None:
    """Decimal from anything numeric. Floats go through str() so their shortest
    repr is used rather than the binary expansion (0.1 → Decimal('0.1'))."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(MONEY, rounding=ROUND_HALF_EVEN)


def qty(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(QTY, rounding=ROUND_DOWN)


def _s(value: Decimal | None, q: Decimal = MONEY) -> str | None:
    """Presentation string; None stays None."""
    if value is None:
        return None
    return str(value.quantize(q, rounding=ROUND_HALF_EVEN if q == MONEY else ROUND_DOWN))


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if not whole else part / whole


# ── Frozen inputs ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Limits:
    """Mandate limits as FRACTIONS of one (0.25 = 25%). Convert at the boundary."""
    max_sector: Decimal
    max_issuer: Decimal
    max_per_trade_of_contribution: Decimal
    max_post_trade_position: Decimal

    @classmethod
    def from_profile(cls, profile) -> "Limits":
        """user_profile stores the first two as percentages (25.0) and the other
        two as fractions (0.4); this is the only place that knows that."""
        return cls(
            max_sector=D(profile.max_sector_pct) / 100,
            max_issuer=D(profile.max_issuer_pct) / 100,
            max_per_trade_of_contribution=D(profile.max_per_candidate_pct_of_cash),
            max_post_trade_position=D(profile.max_post_trade_position_pct),
        )

    def to_dict(self) -> dict:
        return {k: str(v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class Assumptions:
    whole_share_buys: bool = True          # buys round DOWN to whole shares; sells may be fractional up to what is held
    fee_per_trade: Decimal = ZERO          # flat fee per executed trade, in base currency
    base_currency: str = "USD"
    fx_rate_to_base: Decimal = ONE         # single-currency book today; recorded so the field exists

    def to_dict(self) -> dict:
        return {"whole_share_buys": self.whole_share_buys, "fee_per_trade": str(self.fee_per_trade),
                "base_currency": self.base_currency, "fx_rate_to_base": str(self.fx_rate_to_base)}


@dataclass(frozen=True)
class Position:
    account_id: int
    ticker: str
    shares: Decimal
    sector: str | None = None


@dataclass(frozen=True)
class ScenarioInputs:
    """Everything evaluate() reads. Build it with prepare.prepare_inputs()."""
    scope: str                                   # "all" or "account:<id>"
    account_ids: tuple[int, ...]
    account_labels: Mapping[int, str]
    positions: tuple[Position, ...]
    prices: Mapping[str, Decimal]                # pinned close per ticker
    price_as_of: str | None
    starting_cash: Mapping[int, Decimal | None]  # per account; None = unknown
    external_contribution: Mapping[int, Decimal] # per account; new money entering the account
    limits: Limits
    assumptions: Assumptions = Assumptions()
    restricted: frozenset[str] = frozenset()
    excluded_sectors: frozenset[str] = frozenset()
    sector_by_ticker: Mapping[str, str] = field(default_factory=dict)
    issuer_by_ticker: Mapping[str, str] = field(default_factory=dict)   # ticker → issuer key (share classes share one)
    expected_returns: Mapping[str, Decimal] = field(default_factory=dict)  # percentage points over the horizon
    return_horizon_days: int = 21
    return_model: str = "apex-latest-prediction"
    versions: Mapping[str, str] = field(default_factory=dict)  # holdings / predictions / profile / prices / engine
    prepared_at: str | None = None
    warnings: tuple[str, ...] = ()               # data-quality notes from preparation

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "account_ids": list(self.account_ids),
            "account_labels": {str(k): v for k, v in self.account_labels.items()},
            "positions": [{"account_id": p.account_id, "ticker": p.ticker, "shares": _s(p.shares, QTY),
                           "sector": p.sector} for p in self.positions],
            "prices": {t: str(p) for t, p in sorted(self.prices.items())},
            "price_as_of": self.price_as_of,
            "starting_cash": {str(k): _s(v) for k, v in self.starting_cash.items()},
            "external_contribution": {str(k): _s(v) for k, v in self.external_contribution.items()},
            "limits": self.limits.to_dict(),
            "assumptions": self.assumptions.to_dict(),
            "restricted": sorted(self.restricted),
            "excluded_sectors": sorted(self.excluded_sectors),
            "sector_by_ticker": dict(sorted(self.sector_by_ticker.items())),
            "issuer_by_ticker": dict(sorted(self.issuer_by_ticker.items())),
            "expected_returns": {t: str(r) for t, r in sorted(self.expected_returns.items())},
            "return_horizon_days": self.return_horizon_days,
            "return_model": self.return_model,
            "versions": dict(self.versions),
            "prepared_at": self.prepared_at,
            "warnings": list(self.warnings),
        }

    def digest(self) -> str:
        """Content hash of everything that affects evaluation (not prepared_at)."""
        d = self.to_dict(); d.pop("prepared_at", None); d.pop("warnings", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Trade:
    account_id: int
    ticker: str
    side: str                       # "BUY" | "SELL"
    quantity: Decimal | None = None  # exactly one of quantity / amount
    amount: Decimal | None = None    # base-currency notional; converted at the pinned price, rounded down
    source: str = "user"            # "user" (a requirement) | "proposed" (a suggestion)
    why: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"account_id": self.account_id, "ticker": self.ticker, "side": self.side,
                "quantity": _s(self.quantity, QTY), "amount": _s(self.amount), "source": self.source,
                "why": self.why, "meta": dict(self.meta)}


# ── Results ───────────────────────────────────────────────────────────────────

@dataclass
class EvaluatedTrade:
    trade: Trade
    price: Decimal
    quantity: Decimal
    gross: Decimal
    fee: Decimal

    def to_dict(self) -> dict:
        return {**self.trade.to_dict(), "price": str(self.price), "quantity": _s(self.quantity, QTY),
                "gross": _s(self.gross), "fee": _s(self.fee)}


@dataclass
class RejectedTrade:
    trade: Trade
    reason: str
    kind: str                       # "infeasible" | "not_evaluable"

    def to_dict(self) -> dict:
        return {**self.trade.to_dict(), "reason": self.reason, "kind": self.kind}


@dataclass
class ConstraintResult:
    name: str
    status: str                     # pass | fail | not_evaluable
    detail: str
    account_id: int | None = None
    ticker: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ScenarioResult:
    status: str
    trades: list[EvaluatedTrade]
    rejected: list[RejectedTrade]
    constraints: list[ConstraintResult]
    warnings: list[str]
    before: dict
    after: dict
    accounting: dict
    risk: dict
    versions: dict
    inputs_digest: str
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "trades": [t.to_dict() for t in self.trades],
            "rejected": [r.to_dict() for r in self.rejected],
            "constraints": [c.to_dict() for c in self.constraints],
            "warnings": list(self.warnings),
            "before": self.before, "after": self.after,
            "accounting": self.accounting, "risk": self.risk,
            "versions": dict(self.versions), "inputs_digest": self.inputs_digest,
            "engine_version": self.engine_version,
        }


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate(inputs: ScenarioInputs, trades: list[Trade]) -> ScenarioResult:
    prices = inputs.prices
    fee = inputs.assumptions.fee_per_trade
    accounts = list(inputs.account_ids)

    # Book before: shares per (account, ticker); exact Decimals throughout.
    book: dict[int, dict[str, Decimal]] = {a: {} for a in accounts}
    for p in inputs.positions:
        if p.account_id in book:
            book[p.account_id][p.ticker] = book[p.account_id].get(p.ticker, ZERO) + p.shares
    before_book = {a: dict(t) for a, t in book.items()}

    accepted: list[EvaluatedTrade] = []
    rejected: list[RejectedTrade] = []
    flows = {a: {"proceeds": ZERO, "cost": ZERO, "fees": ZERO} for a in accounts}
    bought: dict[int, set[str]] = {a: set() for a in accounts}

    def reject(t: Trade, reason: str, kind: str = "infeasible") -> None:
        rejected.append(RejectedTrade(t, reason, kind))

    # 1. Resolve and apply trades in order (quantities rounded DOWN first).
    for t in trades:
        ticker = (t.ticker or "").upper().strip()
        side = (t.side or "").upper()
        if t.account_id not in book:
            reject(t, f"account {t.account_id} is not in this scenario's scope"); continue
        if side not in ("BUY", "SELL"):
            reject(t, f"unknown side {t.side!r}"); continue
        if ticker in inputs.restricted:
            reject(t, f"{ticker} is on the restricted list"); continue
        sector = inputs.sector_by_ticker.get(ticker)
        if side == "BUY" and sector and sector in inputs.excluded_sectors:
            reject(t, f"{ticker} is in an excluded sector ({sector})"); continue
        price = prices.get(ticker)
        if price is None or price <= 0:
            reject(t, f"no pinned price for {ticker}", NOT_EVALUABLE); continue
        if (t.quantity is None) == (t.amount is None):
            reject(t, "a trade needs exactly one of quantity or amount"); continue

        raw = t.quantity if t.quantity is not None else t.amount / price
        held = book[t.account_id].get(ticker, ZERO)
        if side == "BUY" and inputs.assumptions.whole_share_buys:
            q = raw.to_integral_value(rounding=ROUND_DOWN)
        else:
            q = qty(raw)
        if side == "SELL" and t.amount is not None and q > held:
            q = held                          # "sell $X" of a smaller position means sell it all
        if q <= 0:
            what = f"{_s(t.amount)} {inputs.assumptions.base_currency}" if t.amount is not None else f"{_s(t.quantity, QTY)} shares"
            reject(t, f"{what} rounds down to zero shares of {ticker} at {price}"); continue
        if side == "SELL" and q > held:
            reject(t, f"sell {_s(q, QTY)} {ticker} exceeds the {_s(held, QTY)} held in account {t.account_id}"); continue

        gross = q * price
        if side == "BUY":
            book[t.account_id][ticker] = held + q
            flows[t.account_id]["cost"] += gross
            bought[t.account_id].add(ticker)
        else:
            left = held - q
            if left > 0:
                book[t.account_id][ticker] = left
            else:
                book[t.account_id].pop(ticker, None)
            flows[t.account_id]["proceeds"] += gross
        flows[t.account_id]["fees"] += fee
        accepted.append(EvaluatedTrade(Trade(t.account_id, ticker, side, t.quantity, t.amount, t.source, t.why, t.meta),
                                       price, q, gross, fee))

    # 2. Value both books at the pinned prices.
    constraints: list[ConstraintResult] = []
    warnings: list[str] = list(inputs.warnings)
    unpriced_held = sorted({t for a in accounts for t in set(before_book[a]) | set(book[a]) if prices.get(t) is None})
    if unpriced_held:
        constraints.append(ConstraintResult("prices_available", NOT_EVALUABLE,
                                            f"no pinned price for held position(s): {', '.join(unpriced_held)}; "
                                            "portfolio value and weights exclude them"))
    else:
        constraints.append(ConstraintResult("prices_available", PASS, "every held and traded ticker has a pinned price"))

    def value_of(b: dict[str, Decimal]) -> Decimal:
        return sum((s * prices[t] for t, s in b.items() if t in prices), ZERO)

    accounting: dict[str, Any] = {"accounts": {}}
    totals = {k: ZERO for k in ("starting_cash", "external_contribution", "sale_proceeds", "purchase_cost", "fees",
                                "ending_cash", "starting_positions_value", "ending_positions_value",
                                "starting_portfolio_value", "ending_portfolio_value")}
    cash_known_everywhere = True
    ending_cash: dict[int, Decimal] = {}
    for a in accounts:
        start = inputs.starting_cash.get(a)
        contrib = inputs.external_contribution.get(a, ZERO)
        f = flows[a]
        start_eff = start if start is not None else ZERO
        end = start_eff + contrib + f["proceeds"] - f["cost"] - f["fees"]
        ending_cash[a] = end
        sv, ev = value_of(before_book[a]), value_of(book[a])
        row = {"starting_cash": start, "starting_cash_known": start is not None, "external_contribution": contrib,
               "sale_proceeds": f["proceeds"], "purchase_cost": f["cost"], "fees": f["fees"], "ending_cash": end,
               "starting_positions_value": sv, "ending_positions_value": ev,
               "starting_portfolio_value": sv + start_eff, "ending_portfolio_value": ev + end}
        accounting["accounts"][str(a)] = {k: (v if isinstance(v, bool) else _s(v)) for k, v in row.items()}
        for k in totals:
            totals[k] += row[k] if row[k] is not None else ZERO
        if start is None:
            cash_known_everywhere = False
        # 3a. Cash: never negative, never pooled.
        label = inputs.account_labels.get(a, str(a))
        if start is None:
            if end >= 0:
                constraints.append(ConstraintResult("cash_non_negative", PASS,
                    f"{label}: starting cash unknown; trades are covered by contribution + proceeds "
                    f"({_s(contrib + f['proceeds'])} ≥ {_s(f['cost'] + f['fees'])}), so any starting cash ≥ 0 works", a))
            else:
                constraints.append(ConstraintResult("cash_non_negative", NOT_EVALUABLE,
                    f"{label}: starting cash unknown and trades need {_s(-end)} more than contribution + proceeds; "
                    "set the account's starting cash to evaluate", a))
        elif end < 0:
            constraints.append(ConstraintResult("cash_non_negative", FAIL,
                f"{label}: ending cash would be {_s(end)} (starting {_s(start)} + contribution {_s(contrib)} "
                f"+ proceeds {_s(f['proceeds'])} − purchases {_s(f['cost'])} − fees {_s(f['fees'])})", a))
        else:
            constraints.append(ConstraintResult("cash_non_negative", PASS,
                f"{label}: ending cash {_s(end)} from starting {_s(start)}", a))
        # 3b. Per-trade cap relative to the account's new money.
        lim = inputs.limits.max_per_trade_of_contribution
        for et in accepted:
            if et.trade.account_id != a or et.trade.side != "BUY":
                continue
            if contrib > 0 and et.gross > lim * contrib:
                constraints.append(ConstraintResult("per_trade_cap", FAIL,
                    f"{label}: buying {_s(et.gross)} of {et.trade.ticker} exceeds {_s(lim * 100, MONEY)}% of the "
                    f"{_s(contrib)} contribution ({_s(lim * contrib)})", a, et.trade.ticker))
        if contrib > 0 and not any(c.name == "per_trade_cap" and c.account_id == a and c.status == FAIL for c in constraints):
            constraints.append(ConstraintResult("per_trade_cap", PASS,
                f"{label}: no single buy exceeds {_s(lim * 100, MONEY)}% of the contribution", a))

    identity_cash = totals["ending_cash"] - (totals["starting_cash"] + totals["external_contribution"]
                                             + totals["sale_proceeds"] - totals["purchase_cost"] - totals["fees"])
    identity_value = totals["ending_portfolio_value"] - (totals["starting_portfolio_value"]
                                                         + totals["external_contribution"] - totals["fees"])
    accounting["total"] = {k: _s(v) for k, v in totals.items()}
    accounting["total"]["starting_cash_known_for_all_accounts"] = cash_known_everywhere
    accounting["identity_residual"] = {"cash": _s(identity_cash), "portfolio_value": _s(identity_value)}
    constraints.append(ConstraintResult("accounting_identity", PASS if identity_cash == 0 and identity_value == 0 else FAIL,
                                        "ending cash and portfolio value reconcile exactly to contributions, trades and fees"
                                        if identity_cash == 0 and identity_value == 0 else
                                        f"residual cash {_s(identity_cash)}, value {_s(identity_value)}"))

    # 4. Concentration limits on the post-rounding book, at scope level.
    total_after = totals["ending_portfolio_value"]
    total_before = totals["starting_portfolio_value"]
    scope_after: dict[str, Decimal] = {}
    scope_before: dict[str, Decimal] = {}
    for a in accounts:
        for t, s in book[a].items():
            scope_after[t] = scope_after.get(t, ZERO) + s
        for t, s in before_book[a].items():
            scope_before[t] = scope_before.get(t, ZERO) + s
    all_bought = {t for a in accounts for t in bought[a]}

    def weights(b: dict[str, Decimal], total: Decimal) -> dict[str, Decimal]:
        return {t: (s * prices[t]) / total for t, s in b.items() if t in prices and total}

    w_after, w_before = weights(scope_after, total_after), weights(scope_before, total_before)
    _limit_check(constraints, warnings, "position_limit", w_after, all_bought, inputs.limits.max_post_trade_position,
                 lambda t: t, "position")
    sector_of = {t: inputs.sector_by_ticker.get(t) for t in scope_after}
    unknown_bought = sorted(t for t in all_bought if not sector_of.get(t))
    if unknown_bought:
        constraints.append(ConstraintResult("sector_limit", NOT_EVALUABLE,
                                            f"sector unknown for bought ticker(s): {', '.join(unknown_bought)}"))
    else:
        _limit_check(constraints, warnings, "sector_limit", w_after, all_bought, inputs.limits.max_sector,
                     lambda t: sector_of.get(t) or "Unknown", "sector")
    _limit_check(constraints, warnings, "issuer_limit", w_after, all_bought, inputs.limits.max_issuer,
                 lambda t: inputs.issuer_by_ticker.get(t, t), "issuer")
    n_restricted = sum(1 for r in rejected if "restricted list" in r.reason)
    n_excluded = sum(1 for r in rejected if "excluded sector" in r.reason)
    constraints.append(ConstraintResult("restricted_instruments", PASS,
                                        f"{n_restricted} trade(s) rejected for restricted tickers" if n_restricted else "no restricted tickers traded"))
    constraints.append(ConstraintResult("excluded_sectors", PASS,
                                        f"{n_excluded} buy(s) rejected in excluded sectors" if n_excluded else "no buys in excluded sectors"))
    constraints.append(ConstraintResult("quantity_increments", PASS,
                                        "buys rounded down to whole shares" if inputs.assumptions.whole_share_buys
                                        else "fractional quantities allowed (0.0001)"))

    # 5. Status.
    user_infeasible = [r for r in rejected if r.trade.source == "user" and r.kind == "infeasible"]
    user_not_eval = [r for r in rejected if r.trade.source == "user" and r.kind == NOT_EVALUABLE]
    if user_infeasible or any(c.status == FAIL for c in constraints):
        status = STATUS_INFEASIBLE
    elif user_not_eval or any(c.status == NOT_EVALUABLE for c in constraints):
        status = STATUS_NOT_EVALUABLE
    else:
        status = STATUS_FEASIBLE

    # 6. Before / after books and pinned-input risk estimates.
    def book_view(b: dict[int, dict[str, Decimal]], cash: Mapping[int, Decimal | None]) -> dict:
        out = {"accounts": {}, "total_positions_value": None, "total_cash": None}
        tp = ZERO; tc = ZERO
        for a in accounts:
            pv = value_of(b[a]); c = cash.get(a)
            out["accounts"][str(a)] = {
                "label": inputs.account_labels.get(a, str(a)),
                "cash": _s(c),
                "positions": {t: {"shares": _s(s, QTY), "price": str(prices[t]) if t in prices else None,
                                  "value": _s(s * prices[t]) if t in prices else None}
                              for t, s in sorted(b[a].items())},
                "positions_value": _s(pv),
            }
            tp += pv; tc += c or ZERO
        out["total_positions_value"] = _s(tp); out["total_cash"] = _s(tc)
        return out

    risk = {
        "before": _risk_estimates(w_before, sector_of | {t: inputs.sector_by_ticker.get(t) for t in scope_before},
                                  inputs.expected_returns),
        "after": _risk_estimates(w_after, sector_of, inputs.expected_returns),
        "labels": {"expected_return_pct": {"type": "estimate", "horizon_days": inputs.return_horizon_days,
                                           "model": inputs.return_model,
                                           "prediction_version": inputs.versions.get("predictions")},
                   "weights": "share of scope portfolio value (positions + known cash) at pinned prices"},
    }

    return ScenarioResult(
        status=status, trades=accepted, rejected=rejected, constraints=constraints, warnings=warnings,
        before=book_view(before_book, inputs.starting_cash), after=book_view(book, ending_cash),
        accounting=accounting, risk=risk, versions=dict(inputs.versions) | {"engine": ENGINE_VERSION},
        inputs_digest=inputs.digest(),
    )


def _limit_check(constraints, warnings, name, w_after, bought, limit, group_of, noun) -> None:
    """Fail when a group that RECEIVED a buy ends above the limit; a group that was
    already above it and untouched is a warning, since the trades did not cause it."""
    group_w: dict[str, Decimal] = {}
    bought_groups: set[str] = set()
    for t, w in w_after.items():
        g = group_of(t)
        group_w[g] = group_w.get(g, ZERO) + w
        if t in bought:
            bought_groups.add(g)
    failed = False
    for g, w in sorted(group_w.items()):
        if w <= limit:
            continue
        msg = f"{noun} {g} would be {_s(w * 100, MONEY)}% of the portfolio (limit {_s(limit * 100, MONEY)}%)"
        if g in bought_groups:
            constraints.append(ConstraintResult(name, FAIL, msg, ticker=g if noun == "position" else None)); failed = True
        else:
            warnings.append(f"{msg}; already above the limit before these trades, untouched")
    if not failed:
        constraints.append(ConstraintResult(name, PASS, f"no {noun} that received a buy exceeds {_s(limit * 100, MONEY)}%"))


def _risk_estimates(w: dict[str, Decimal], sector_of: Mapping[str, str | None], er: Mapping[str, Decimal]) -> dict:
    sectors: dict[str, Decimal] = {}
    for t, x in w.items():
        s = sector_of.get(t) or "Unknown"
        sectors[s] = sectors.get(s, ZERO) + x
    top_sector = max(sectors, key=sectors.get) if sectors else None
    top_pos = max(w, key=w.get) if w else None
    total_w = sum(w.values(), ZERO)
    covered = [(x, er[t]) for t, x in w.items() if t in er]
    cov_w = sum((x for x, _ in covered), ZERO)
    exp_ret = (sum((x * r for x, r in covered), ZERO) / cov_w) if cov_w else None
    return {
        "positions": len(w),
        "top_position": top_pos, "top_position_pct": _s(w[top_pos] * 100, MONEY) if top_pos else None,
        "top_sector": top_sector, "top_sector_pct": _s(sectors[top_sector] * 100, MONEY) if top_sector else None,
        "expected_return_pct": _s(exp_ret, MONEY) if exp_ret is not None else None,
        "expected_return_coverage_pct": _s(cov_w / total_w * 100, MONEY) if total_w else None,   # share of positions value with an estimate
    }
