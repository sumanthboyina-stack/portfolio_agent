"""
Investor settings — global, admin-only, database-backed. The engine_settings_db pattern: the constants below are SEED
DEFAULTS only; the values the application uses at runtime come from these tables, so an administrator can change them
without a code change or deploy. Rows are versioned (version, updated_at, updated_by), updates take an
expected_version, and there is a reset-to-defaults function for each table. No file of any kind holds a setting.

Tables
  risk_quiz_settings      single row (id = 1): the optional 3-question quiz — question text, options and point
                          values, the cap rule (the "when will you need it" answer caps the profile) and the
                          preference thresholds. quiz_version bumps whenever any of those three change.
  risk_profile_settings   one row per profile (CONSERVATIVE / MODERATE / AGGRESSIVE): description, target allocation
                          bands, the numeric limits a 'profile' limits_source user gets, and the instrument types
                          that profile allows.
  portfolio_settings      single row (id = 1): portfolio-wide defaults — the default emergency cash reserve ($3,000),
                          how long fund data stays fresh (ETF / mutual fund), the look-through coverage threshold and
                          depth, the "high expense ratio" cut-off, and whether health snapshots are taken. Columns
                          after the first are added with migrate_columns (never by rebuilding the table), so an
                          existing row — including its reserve — is never touched.

This module is the data layer only (no RequestContext): the admin-only check and the audit event live in
services.investor_service, which is the only place the application writes these tables.

Privacy: nothing here is about any one person. A person's profile, reserve and goals are the OWNER-scoped tables in
tools.investor_profile_db.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import (
    ALLOCATION_ASSET_CLASSES, INSTRUMENT_TYPES, RISK_PROFILES, InstrumentType, RiskProfile,
)
from portfolio_agent.tools.db import db_conn, migrate_columns

# ── Seed defaults (code constants: used ONLY to create / reset rows) ───────────

DEFAULT_EMERGENCY_RESERVE_AMOUNT = 3000.00

# Fund-data / look-through / fees / health settings (seed defaults for the extra portfolio_settings columns).
PORTFOLIO_SETTINGS_EXTRA_DEFAULTS: dict = {
    "etf_data_ttl_hours": 24,              # re-fetch an ETF's holdings after this many hours
    "mutual_fund_data_ttl_hours": 168,     # mutual funds / target-date funds publish less often: a week
    "lookthrough_min_coverage_pct": 80.0,  # use look-through only when it explains at least this % of the portfolio
    "lookthrough_max_depth": 3,            # fund-of-funds levels to open
    "high_expense_ratio_pct": 0.50,        # a fund costing more than this % a year is "high"
    "health_snapshot_enabled": 1,          # take one portfolio-health snapshot per owner per day
}

# Portfolio Health thresholds (seed defaults for more portfolio_settings columns): name -> (default, min, max).
# Nothing in tools/portfolio_health.py decides a status from a number that is not one of these or a policy limit.
HEALTH_SETTINGS_SPEC: dict = {
    "health_action_band_multiple":        (2.0, 1.0, 10.0),    # ACTION when outside a target band by this many band-widths
    "health_watch_limit_fraction":        (0.8, 0.0, 1.0),     # WATCH when a concentration reaches this fraction of its limit
    "health_min_effective_n_watch":       (8.0, 1.0, 1000.0),  # effective number of holdings below this: WATCH
    "health_min_effective_n_action":      (4.0, 1.0, 1000.0),  # …below this: ACTION
    "health_max_avg_correlation_watch":   (0.70, 0.0, 1.0),    # average correlation with the rest of the portfolio
    "health_max_avg_correlation_action":  (0.90, 0.0, 1.0),
    "health_unknown_share_tolerance_pct": (10.0, 0.0, 100.0),  # more than this % of a mix unknown: the check cannot be GOOD
    "health_unpriced_action_pct":         (25.0, 0.0, 100.0),  # holdings without a price above this % of value: ACTION
    "health_stale_fund_data_multiple":    (2.0, 1.0, 100.0),   # fund data older than this many refresh intervals is stale
}
HEALTH_SETTINGS_DEFAULTS: dict = {k: v[0] for k, v in HEALTH_SETTINGS_SPEC.items()}

SEED_QUIZ_QUESTIONS: list[dict] = [
    {"id": "horizon", "text": "When will you need most of this money?", "default_option": "3to10",
     "options": [{"key": "lt3", "label": "Less than 3 years", "points": 0},
                 {"key": "3to10", "label": "3–10 years", "points": 1},
                 {"key": "10plus", "label": "10+ years", "points": 2}]},
    {"id": "drop", "text": "If your portfolio fell 20% in a month, you'd most likely…", "default_option": "wait",
     "options": [{"key": "sell", "label": "Sell some to stop the losses", "points": 0},
                 {"key": "wait", "label": "Wait it out", "points": 1},
                 {"key": "buy", "label": "Buy more while prices are low", "points": 2}]},
    {"id": "priority", "text": "What matters most?", "default_option": "balance",
     "options": [{"key": "protect", "label": "Protect what I have", "points": 0},
                 {"key": "balance", "label": "Balance growth and safety", "points": 1},
                 {"key": "grow", "label": "Grow as much as possible", "points": 2}]},
]
# Q1 sets a CAP (what the person's situation can handle); the other questions set a PREFERENCE.
SEED_QUIZ_CAP_RULES: dict = {
    "question_id": "horizon",
    "cap_by_option": {"lt3": "CONSERVATIVE", "3to10": "MODERATE", "10plus": "AGGRESSIVE"},
}
SEED_QUIZ_PREFERENCE_THRESHOLDS: dict = {
    "question_ids": ["drop", "priority"],
    "bands": [{"max_points": 1, "profile": "CONSERVATIVE"},      # sum 0–1
              {"max_points": 3, "profile": "MODERATE"},          # sum 2–3
              {"max_points": 4, "profile": "AGGRESSIVE"}],       # sum 4
}


def _band(target: float, pts: float) -> dict:
    return {"target": target, "min": max(0.0, target - pts), "max": min(100.0, target + pts)}


def _seed_profile(display_name: str, description: str, us: float, intl: float, fi: float, cash: float,
                  pos: float, issuer: float, sector: float, lev: float, allowed: list[str]) -> dict:
    return {
        "display_name": display_name, "description": description,
        "allocation": {"US_EQUITY": _band(us, 5), "INTL_EQUITY": _band(intl, 5),
                       "FIXED_INCOME": _band(fi, 5), "CASH": _band(cash, 3)},
        "max_position_pct": pos, "max_issuer_pct": issuer, "max_sector_pct": sector,
        "max_candidate_pct_of_cash": 40.0, "max_leveraged_inverse_pct": lev,
        "allowed_instrument_types": allowed,
    }


_ALL_TYPES = [m.value for m in InstrumentType]
SEED_RISK_PROFILES: dict[str, dict] = {
    RiskProfile.CONSERVATIVE.value: _seed_profile(
        "Conservative", "Protect what you have. Mostly bonds and cash, with a smaller share in stocks; small positions, "
        "no leveraged funds.", 25, 5, 55, 15, 8, 8, 20, 0, [t for t in _ALL_TYPES if t != "ETN"]),
    RiskProfile.MODERATE.value: _seed_profile(
        "Moderate", "Balance growth and safety. A stock-heavy mix with a solid bond cushion; the default for everyone.",
        45, 15, 33, 7, 12, 12, 25, 5, list(_ALL_TYPES)),
    RiskProfile.AGGRESSIVE.value: _seed_profile(
        "Aggressive", "Grow as much as possible. Mostly stocks, including international, with little in bonds or cash; "
        "larger positions and some room for leveraged funds.", 62, 25, 10, 3, 20, 20, 35, 15, list(_ALL_TYPES)),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SettingsVersionConflict(RuntimeError):
    """The settings changed since the caller loaded them; reload and retry."""


# ── Connection / schema / seed ────────────────────────────────────────────────

_schema_ready: set[str] = set()


def _ensure_once() -> None:
    """Create + seed the tables once per process per database file. Unlike engine_settings_db this does NOT re-run
    DDL and the seed insert on every connection: the effective policy reads these tables on every exposures call."""
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        def _setup(conn: sqlite3.Connection) -> None:
            _create_schema(conn)
            _seed(conn)
            conn.commit()
        with db_conn(setup=_setup):
            pass
        _schema_ready.add(key)


@contextmanager
def _db(conn: sqlite3.Connection | None = None):
    """The caller's connection (never committed here — the caller owns the transaction, e.g. a service writing the
    setting and its audit event together), or a fresh one. A fresh connection is committed by the writers."""
    _ensure_once()
    if conn is not None:
        yield conn
        return
    with db_conn() as c:
        yield c


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_quiz_settings (
            id                          INTEGER PRIMARY KEY CHECK (id = 1),
            questions_json              TEXT NOT NULL,
            cap_rules_json              TEXT NOT NULL,
            preference_thresholds_json  TEXT NOT NULL,
            quiz_version                INTEGER NOT NULL DEFAULT 1,
            version                     INTEGER NOT NULL DEFAULT 1,
            created_at                  TEXT NOT NULL,
            updated_at                  TEXT NOT NULL,
            updated_by                  TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_profile_settings (
            profile                         TEXT PRIMARY KEY
                                              CHECK (profile IN ('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE')),
            display_name                    TEXT NOT NULL,
            description                     TEXT NOT NULL,
            allocation_json                 TEXT NOT NULL,
            max_position_pct                REAL NOT NULL,
            max_issuer_pct                  REAL NOT NULL,
            max_sector_pct                  REAL NOT NULL,
            max_candidate_pct_of_cash       REAL NOT NULL,
            max_leveraged_inverse_pct       REAL NOT NULL,
            allowed_instrument_types_json   TEXT NOT NULL,
            version                         INTEGER NOT NULL DEFAULT 1,
            updated_at                      TEXT NOT NULL,
            updated_by                      TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_settings (
            id                                INTEGER PRIMARY KEY CHECK (id = 1),
            default_emergency_reserve_amount  REAL NOT NULL CHECK (default_emergency_reserve_amount >= 0),
            version                           INTEGER NOT NULL DEFAULT 1,
            created_at                        TEXT NOT NULL,
            updated_at                        TEXT NOT NULL,
            updated_by                        TEXT
        )""")
    # Columns after the first are added with migrate_columns, so an existing row keeps every value it has (including
    # default_emergency_reserve_amount) and just gains these with their defaults.
    migrate_columns(conn, "portfolio_settings", [
        ("etf_data_ttl_hours",           "REAL NOT NULL DEFAULT 24"),
        ("mutual_fund_data_ttl_hours",   "REAL NOT NULL DEFAULT 168"),
        ("lookthrough_min_coverage_pct", "REAL NOT NULL DEFAULT 80"),
        ("lookthrough_max_depth",        "INTEGER NOT NULL DEFAULT 3"),
        ("high_expense_ratio_pct",       "REAL NOT NULL DEFAULT 0.5"),
        ("health_snapshot_enabled",      "INTEGER NOT NULL DEFAULT 1"),
    ] + [(name, f"REAL NOT NULL DEFAULT {spec[0]}") for name, spec in HEALTH_SETTINGS_SPEC.items()])


def _seed(conn: sqlite3.Connection) -> None:
    """Insert any missing row from the seed constants. NEVER overwrites an existing row."""
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO risk_quiz_settings
               (id, questions_json, cap_rules_json, preference_thresholds_json, quiz_version, created_at, updated_at)
           VALUES (1, ?, ?, ?, 1, ?, ?)""",
        [json.dumps(SEED_QUIZ_QUESTIONS), json.dumps(SEED_QUIZ_CAP_RULES), json.dumps(SEED_QUIZ_PREFERENCE_THRESHOLDS),
         now, now])
    for profile, p in SEED_RISK_PROFILES.items():
        conn.execute(
            """INSERT OR IGNORE INTO risk_profile_settings
                   (profile, display_name, description, allocation_json, max_position_pct, max_issuer_pct,
                    max_sector_pct, max_candidate_pct_of_cash, max_leveraged_inverse_pct,
                    allowed_instrument_types_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [profile, p["display_name"], p["description"], json.dumps(p["allocation"]), p["max_position_pct"],
             p["max_issuer_pct"], p["max_sector_pct"], p["max_candidate_pct_of_cash"], p["max_leveraged_inverse_pct"],
             json.dumps(p["allowed_instrument_types"]), now])
    conn.execute(
        """INSERT OR IGNORE INTO portfolio_settings
               (id, default_emergency_reserve_amount, created_at, updated_at) VALUES (1, ?, ?, ?)""",
        [DEFAULT_EMERGENCY_RESERVE_AMOUNT, now, now])
    _migrate_allowed_types(conn)


def _migrate_allowed_types(conn: sqlite3.Connection) -> None:
    """
    STABLE_VALUE was added after the profiles were first seeded. A profile whose allowed-types list is still exactly
    the old seed (every type, or every type but ETN for CONSERVATIVE) gets STABLE_VALUE appended, so it is not
    suddenly "not allowed". A list an administrator has customised is left alone. Idempotent; not a settings edit,
    so version / updated_by are untouched.
    """
    new = InstrumentType.STABLE_VALUE.value
    old_all = {m.value for m in InstrumentType if m.value != new}
    for profile, raw in conn.execute("SELECT profile, allowed_instrument_types_json FROM risk_profile_settings").fetchall():
        types = json.loads(raw)
        if new in types:
            continue
        seeded_old = old_all - ({"ETN"} if profile == RiskProfile.CONSERVATIVE.value else set())
        if set(types) == seeded_old:
            conn.execute("UPDATE risk_profile_settings SET allowed_instrument_types_json = ? WHERE profile = ?",
                         [json.dumps([*types, new]), profile])


# ── Validation ────────────────────────────────────────────────────────────────

def _num(value, what: str, lo: float, hi: float | None = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: must be a number, got {value!r}")
    if not math.isfinite(v) or v < lo or (hi is not None and v > hi):
        raise ValueError(f"{what}: must be between {lo:g} and {hi:g}, got {v:g}" if hi is not None
                         else f"{what}: must be at least {lo:g}, got {v:g}")
    return v


def validate_allocation(allocation: dict, *, what: str = "allocation") -> dict:
    """
    {asset_class: {target, min, max}} in percentage points. Only AssetClass values a portfolio can be allocated to
    (MULTI_ASSET and UNKNOWN are looked through, never targeted); every number 0–100; min <= target <= max; targets
    sum to 100. Returns a clean copy with floats. Raises ValueError. Shared by the profile defaults and a user's
    custom allocation.
    """
    if not isinstance(allocation, dict) or not allocation:
        raise ValueError(f"{what}: at least one asset class is required")
    bad = sorted(set(allocation) - set(ALLOCATION_ASSET_CLASSES))
    if bad:
        raise ValueError(f"{what}: {bad} cannot be a target (allowed: {list(ALLOCATION_ASSET_CLASSES)}; "
                         "MULTI_ASSET and UNKNOWN are looked through)")
    clean: dict = {}
    for cls, row in allocation.items():
        if not isinstance(row, dict) or not {"target", "min", "max"} <= set(row):
            raise ValueError(f"{what}[{cls}]: needs target, min and max")
        t, lo, hi = (_num(row[k], f"{what}[{cls}].{k}", 0.0, 100.0) for k in ("target", "min", "max"))
        if not (lo <= t <= hi):
            raise ValueError(f"{what}[{cls}]: must satisfy min <= target <= max (got {lo:g} / {t:g} / {hi:g})")
        clean[cls] = {"target": t, "min": lo, "max": hi}
    total = sum(r["target"] for r in clean.values())
    if abs(total - 100.0) > 0.01:
        raise ValueError(f"{what}: targets must sum to 100 (got {total:g})")
    return clean


def _validate_quiz(questions, cap_rules, thresholds) -> None:
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions: at least one question is required")
    ids, by_id = [], {}
    for q in questions:
        qid = (q or {}).get("id")
        if not qid or not (q.get("text") or "").strip():
            raise ValueError("questions: every question needs an id and text")
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            raise ValueError(f"questions[{qid}]: needs at least two options")
        keys = [o.get("key") for o in opts]
        if any(not k for k in keys) or len(set(keys)) != len(keys):
            raise ValueError(f"questions[{qid}]: option keys must be present and unique")
        for o in opts:
            if not (o.get("label") or "").strip():
                raise ValueError(f"questions[{qid}]: every option needs a label")
            _num(o.get("points"), f"questions[{qid}].{o.get('key')}.points", 0.0)
        if q.get("default_option") not in keys:
            raise ValueError(f"questions[{qid}]: default_option must be one of its option keys (the 'middle' answer)")
        ids.append(qid)
        by_id[qid] = q
    if len(set(ids)) != len(ids):
        raise ValueError("questions: ids must be unique")

    cap_q = (cap_rules or {}).get("question_id")
    if cap_q not in by_id:
        raise ValueError(f"cap_rules.question_id {cap_q!r} is not a question")
    cap_map = (cap_rules or {}).get("cap_by_option") or {}
    cap_keys = {o["key"] for o in by_id[cap_q]["options"]}
    if set(cap_map) != cap_keys or any(v not in RISK_PROFILES for v in cap_map.values()):
        raise ValueError("cap_rules.cap_by_option: must map every option of the cap question to a profile")

    pref_ids = (thresholds or {}).get("question_ids") or []
    if not pref_ids or any(i not in by_id for i in pref_ids) or cap_q in pref_ids:
        raise ValueError("preference_thresholds.question_ids: must be existing questions other than the cap question")
    bands = (thresholds or {}).get("bands") or []
    if not bands or any(b.get("profile") not in RISK_PROFILES for b in bands):
        raise ValueError("preference_thresholds.bands: each band needs a valid profile")
    maxes = [_num(b.get("max_points"), "preference_thresholds.bands.max_points", 0.0) for b in bands]
    if maxes != sorted(set(maxes)):
        raise ValueError("preference_thresholds.bands: max_points must be strictly increasing")
    top = sum(max(o["points"] for o in by_id[i]["options"]) for i in pref_ids)
    if maxes[-1] < top:
        raise ValueError(f"preference_thresholds.bands: the last band must reach the highest possible score ({top:g})")


# ── Row <-> dict ──────────────────────────────────────────────────────────────

def _quiz_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    return {**{k: d[k] for k in ("quiz_version", "version", "created_at", "updated_at", "updated_by")},
            "questions": json.loads(d["questions_json"]), "cap_rules": json.loads(d["cap_rules_json"]),
            "preference_thresholds": json.loads(d["preference_thresholds_json"])}


def _profile_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["allocation"] = json.loads(d.pop("allocation_json"))
    d["allowed_instrument_types"] = json.loads(d.pop("allowed_instrument_types_json"))
    return d


# ── Quiz settings ─────────────────────────────────────────────────────────────

def get_quiz_settings(*, conn: sqlite3.Connection | None = None) -> dict:
    with _db(conn) as conn:
        return _quiz_row(conn.execute("SELECT * FROM risk_quiz_settings WHERE id = 1").fetchone())


def update_quiz_settings(*, updated_by: str, expected_version: int | None = None, questions: list | None = None,
                         cap_rules: dict | None = None, preference_thresholds: dict | None = None,
                         conn: sqlite3.Connection | None = None) -> dict:
    """Change the quiz. The three parts are validated TOGETHER (they refer to each other); quiz_version bumps, so
    answers recorded under an older quiz stay identifiable."""
    if questions is None and cap_rules is None and preference_thresholds is None:
        return get_quiz_settings(conn=conn)
    cur = get_quiz_settings(conn=conn)
    q = cur["questions"] if questions is None else questions
    c = cur["cap_rules"] if cap_rules is None else cap_rules
    t = cur["preference_thresholds"] if preference_thresholds is None else preference_thresholds
    _validate_quiz(q, c, t)
    return _write_quiz(q, c, t, updated_by, expected_version, conn)


def _write_quiz(q, c, t, updated_by: str, expected_version: int | None, conn: sqlite3.Connection | None = None) -> dict:
    sql = ("UPDATE risk_quiz_settings SET questions_json = ?, cap_rules_json = ?, preference_thresholds_json = ?, "
           "quiz_version = quiz_version + 1, version = version + 1, updated_at = ?, updated_by = ? WHERE id = 1")
    params: list = [json.dumps(q), json.dumps(c), json.dumps(t), _now(), updated_by]
    if expected_version is not None:
        sql += " AND version = ?"; params.append(expected_version)
    with _db(conn) as cx:
        n = cx.execute(sql, params).rowcount
        if conn is None:
            cx.commit()
        if n == 0:
            raise SettingsVersionConflict("quiz settings changed since you loaded them; reload and retry")
        return get_quiz_settings(conn=cx)


def reset_quiz_settings_to_defaults(*, updated_by: str, conn: sqlite3.Connection | None = None) -> dict:
    return _write_quiz(SEED_QUIZ_QUESTIONS, SEED_QUIZ_CAP_RULES, SEED_QUIZ_PREFERENCE_THRESHOLDS, updated_by, None, conn)


# ── Risk profile settings ─────────────────────────────────────────────────────

_PROFILE_NUMERIC = ("max_position_pct", "max_issuer_pct", "max_sector_pct", "max_candidate_pct_of_cash",
                    "max_leveraged_inverse_pct")


def get_risk_profile_settings(profile: str | None = None, *, conn: sqlite3.Connection | None = None):
    """One profile's settings dict, or {profile: settings} for all three when profile is None."""
    with _db(conn) as conn:
        if profile is not None:
            row = conn.execute("SELECT * FROM risk_profile_settings WHERE profile = ?", [profile]).fetchone()
            if row is None:
                raise ValueError(f"unknown profile {profile!r}; expected one of {list(RISK_PROFILES)}")
            return _profile_row(row)
        rows = conn.execute("SELECT * FROM risk_profile_settings").fetchall()
    out = {r["profile"]: _profile_row(r) for r in rows}
    return {p: out[p] for p in RISK_PROFILES}


def update_risk_profile_settings(profile: str, *, updated_by: str, expected_version: int | None = None,
                                 display_name: str | None = None, description: str | None = None,
                                 allocation: dict | None = None, max_position_pct: float | None = None,
                                 max_issuer_pct: float | None = None, max_sector_pct: float | None = None,
                                 max_candidate_pct_of_cash: float | None = None,
                                 max_leveraged_inverse_pct: float | None = None,
                                 allowed_instrument_types: list[str] | None = None,
                                 conn: sqlite3.Connection | None = None) -> dict:
    """Validated update of one profile's row; raises ValueError on bad input and SettingsVersionConflict when
    expected_version is stale."""
    if profile not in RISK_PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {list(RISK_PROFILES)}")
    fields: dict = {}
    if display_name is not None:
        if not display_name.strip():
            raise ValueError("display_name cannot be blank")
        fields["display_name"] = display_name.strip()
    if description is not None:
        fields["description"] = description.strip()
    if allocation is not None:
        fields["allocation_json"] = json.dumps(validate_allocation(allocation, what=f"{profile} allocation"))
    for name, val in zip(_PROFILE_NUMERIC, (max_position_pct, max_issuer_pct, max_sector_pct,
                                            max_candidate_pct_of_cash, max_leveraged_inverse_pct)):
        if val is not None:
            fields[name] = _num(val, name, 0.0, 100.0)
    if allowed_instrument_types is not None:
        types = list(dict.fromkeys(allowed_instrument_types))
        bad = sorted(set(types) - INSTRUMENT_TYPES)
        if bad or not types:
            raise ValueError(f"allowed_instrument_types: need at least one valid type; unknown: {bad}")
        fields["allowed_instrument_types_json"] = json.dumps(types)
    if not fields:
        return get_risk_profile_settings(profile, conn=conn)
    return _write_profile(profile, fields, updated_by, expected_version, conn)


def _write_profile(profile: str, fields: dict, updated_by: str, expected_version: int | None,
                   conn: sqlite3.Connection | None = None) -> dict:
    fields = {**fields, "updated_at": _now(), "updated_by": updated_by}
    sql = (f"UPDATE risk_profile_settings SET {', '.join(f'{c} = ?' for c in fields)}, version = version + 1 "
           "WHERE profile = ?")
    params = [*fields.values(), profile]
    if expected_version is not None:
        sql += " AND version = ?"; params.append(expected_version)
    with _db(conn) as cx:
        n = cx.execute(sql, params).rowcount
        if conn is None:
            cx.commit()
        if n == 0:
            raise SettingsVersionConflict(f"{profile} settings changed since you loaded them; reload and retry")
        return get_risk_profile_settings(profile, conn=cx)


def reset_risk_profile_settings_to_defaults(*, updated_by: str, profile: str | None = None,
                                            conn: sqlite3.Connection | None = None) -> dict:
    """Restore the seed numbers for one profile (or all three). Returns {profile: settings}."""
    targets = [profile] if profile else list(RISK_PROFILES)
    for p in targets:
        if p not in SEED_RISK_PROFILES:
            raise ValueError(f"unknown profile {p!r}")
        seed = SEED_RISK_PROFILES[p]
        _write_profile(p, {
            "display_name": seed["display_name"], "description": seed["description"],
            "allocation_json": json.dumps(seed["allocation"]),
            **{n: seed[n] for n in _PROFILE_NUMERIC},
            "allowed_instrument_types_json": json.dumps(seed["allowed_instrument_types"]),
        }, updated_by, None, conn)
    return {p: get_risk_profile_settings(p, conn=conn) for p in targets}


# ── Portfolio settings ────────────────────────────────────────────────────────

def get_portfolio_settings(*, conn: sqlite3.Connection | None = None) -> dict:
    with _db(conn) as cx:
        return dict(cx.execute("SELECT * FROM portfolio_settings WHERE id = 1").fetchone())


def update_portfolio_settings(*, updated_by: str, expected_version: int | None = None,
                              default_emergency_reserve_amount: float | None = None,
                              etf_data_ttl_hours: float | None = None,
                              mutual_fund_data_ttl_hours: float | None = None,
                              lookthrough_min_coverage_pct: float | None = None,
                              lookthrough_max_depth: int | None = None,
                              high_expense_ratio_pct: float | None = None,
                              health_snapshot_enabled: bool | None = None,
                              conn: sqlite3.Connection | None = None, **health_thresholds) -> dict:
    """
    default_emergency_reserve_amount: dollars, >= 0 — the reserve every user without their own amount gets.
    etf_data_ttl_hours / mutual_fund_data_ttl_hours: how long stored fund data counts as fresh (> 0).
    lookthrough_min_coverage_pct: 0–100. lookthrough_max_depth: whole levels, 1–6.
    high_expense_ratio_pct: a fund's annual cost above this percent is "high" (0–10).
    health_snapshot_enabled: whether a daily portfolio-health snapshot is taken.
    health_thresholds: any of HEALTH_SETTINGS_SPEC's names, each within its own range (the two effective-N and the two
    correlation levels must keep WATCH on the safe side of ACTION).
    """
    fields: dict = {}
    unknown = set(health_thresholds) - set(HEALTH_SETTINGS_SPEC)
    if unknown:
        raise ValueError(f"unknown portfolio setting(s): {', '.join(sorted(unknown))}")
    for name, val in health_thresholds.items():
        if val is not None:
            _, lo, hi = HEALTH_SETTINGS_SPEC[name]
            fields[name] = _num(val, name, lo, hi)
    if fields:
        cur = get_portfolio_settings(conn=conn)
        eff = {k: fields.get(k, cur[k]) for k in HEALTH_SETTINGS_SPEC}
        if eff["health_min_effective_n_action"] > eff["health_min_effective_n_watch"]:
            raise ValueError("health_min_effective_n_action must not exceed health_min_effective_n_watch")
        if eff["health_max_avg_correlation_action"] < eff["health_max_avg_correlation_watch"]:
            raise ValueError("health_max_avg_correlation_action must not be below health_max_avg_correlation_watch")
    if default_emergency_reserve_amount is not None:
        fields["default_emergency_reserve_amount"] = _num(default_emergency_reserve_amount,
                                                          "default_emergency_reserve_amount", 0.0)
    for name, val in (("etf_data_ttl_hours", etf_data_ttl_hours), ("mutual_fund_data_ttl_hours", mutual_fund_data_ttl_hours)):
        if val is not None:
            v = _num(val, name, 0.0, 24.0 * 365)
            if v <= 0:
                raise ValueError(f"{name}: must be greater than 0")
            fields[name] = v
    if lookthrough_min_coverage_pct is not None:
        fields["lookthrough_min_coverage_pct"] = _num(lookthrough_min_coverage_pct, "lookthrough_min_coverage_pct", 0.0, 100.0)
    if lookthrough_max_depth is not None:
        d = _num(lookthrough_max_depth, "lookthrough_max_depth", 1.0, 6.0)
        if d != int(d):
            raise ValueError("lookthrough_max_depth: must be a whole number")
        fields["lookthrough_max_depth"] = int(d)
    if high_expense_ratio_pct is not None:
        fields["high_expense_ratio_pct"] = _num(high_expense_ratio_pct, "high_expense_ratio_pct", 0.0, 10.0)
    if health_snapshot_enabled is not None:
        fields["health_snapshot_enabled"] = 1 if health_snapshot_enabled else 0
    if not fields:
        return get_portfolio_settings(conn=conn)
    return _write_portfolio(fields, updated_by, expected_version, conn)


def _write_portfolio(fields: dict, updated_by: str, expected_version: int | None,
                     conn: sqlite3.Connection | None = None) -> dict:
    fields = {**fields, "updated_at": _now(), "updated_by": updated_by}
    sql = f"UPDATE portfolio_settings SET {', '.join(f'{c} = ?' for c in fields)}, version = version + 1 WHERE id = 1"
    params = list(fields.values())
    if expected_version is not None:
        sql += " AND version = ?"; params.append(expected_version)
    with _db(conn) as cx:
        n = cx.execute(sql, params).rowcount
        if conn is None:
            cx.commit()
        if n == 0:
            raise SettingsVersionConflict("portfolio settings changed since you loaded them; reload and retry")
        return get_portfolio_settings(conn=cx)


def reset_portfolio_settings_to_defaults(*, updated_by: str, conn: sqlite3.Connection | None = None) -> dict:
    return _write_portfolio({"default_emergency_reserve_amount": DEFAULT_EMERGENCY_RESERVE_AMOUNT,
                             **PORTFOLIO_SETTINGS_EXTRA_DEFAULTS, **HEALTH_SETTINGS_DEFAULTS}, updated_by, None, conn)
