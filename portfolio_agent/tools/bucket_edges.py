"""
Horizon-scaled return buckets — the ONE place that says where "flat", "moderate" and "strong" begin for a given horizon.

A 5-day move of +6% is a strong move; a 250-day move of +6% is not. So the three cut-offs scale with the horizon:

    flat         |return| <= flat          is "flat"   (the UP / DOWN / FLAT classes and the middle bucket)
    strong       |return| >  strong        is "strong" (strong_down / strong_up; between flat and strong is "moderate")
    significant  |return| >  significant   makes a wrong direction call "wrong_significant" rather than "wrong_minor"

with flat < significant < strong, all positive. The five buckets are strong_down, moderate_down, flat, moderate_up, strong_up.

VERSIONS. The cut-offs are an admin setting (engine_settings.horizon_bucket_edges_json) and every change bumps
engine_settings.bucket_edges_version. A prediction stores the version it was MADE under (predictions.bucket_edges_version)
and is ALWAYS scored with those edges — never re-scored under newer ones — because the model was told those ranges in its
prompt. Version 1 is the original fixed ±1% / ±5% / 2% for EVERY horizon; rows that predate this feature are version 1, and
the 5-day edges of every later version equal version 1, so existing 5-day results do not move. Every version ever used is
kept, append-only, in bucket_edges_history, so any past row can be rescored exactly.

Everything below is pure except edges_for() with a version above 1, which reads the (immutable, cached) history.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

LEGACY_VERSION = 1
SUPPORTED_HORIZONS = (5, 21, 63, 250)
BUCKET_NAMES = ("strong_down", "moderate_down", "flat", "moderate_up", "strong_up")


@dataclass(frozen=True)
class Edges:
    flat: float            # fraction, e.g. 0.01 = 1%
    strong: float
    significant: float

    def to_dict(self) -> dict:
        return {"flat": self.flat, "strong": self.strong, "significant": self.significant}


LEGACY_EDGES = Edges(flat=0.01, strong=0.05, significant=0.02)         # version 1, every horizon
# Seed for the CURRENT edges (version 2): 5d is exactly version 1.
SEED_EDGES: dict[int, Edges] = {
    5: Edges(0.01, 0.05, 0.02),
    21: Edges(0.02, 0.08, 0.04),
    63: Edges(0.04, 0.12, 0.07),
    250: Edges(0.08, 0.25, 0.15),
}
SEED_VERSION = 2
LEGACY_TABLE: dict[int, Edges] = {h: LEGACY_EDGES for h in SUPPORTED_HORIZONS}

# The module-level constants the rest of the code kept using (5-day, version 1): backward compatibility only.
FLAT_THRESHOLD = LEGACY_EDGES.flat


# ── Table <-> JSON, validation ────────────────────────────────────────────────

def table_to_json(table: dict[int, Edges]) -> str:
    return json.dumps({str(h): e.to_dict() for h, e in sorted(table.items())})


def table_from_json(raw: str) -> dict[int, Edges]:
    return {int(h): Edges(float(v["flat"]), float(v["strong"]), float(v["significant"])) for h, v in json.loads(raw).items()}


def validate_table(table: dict) -> dict[int, Edges]:
    """{horizon: {flat, strong, significant}} -> {horizon: Edges}. Every supported horizon must be present; for each,
    0 < flat < significant < strong, all finite. Raises ValueError with the horizon and the rule."""
    out: dict[int, Edges] = {}
    for h in SUPPORTED_HORIZONS:
        raw = table.get(h, table.get(str(h)))
        if raw is None:
            raise ValueError(f"bucket edges: horizon {h}d is missing")
        e = raw if isinstance(raw, Edges) else Edges(*(raw.get(k) for k in ("flat", "strong", "significant")))
        for name, v in (("flat", e.flat), ("strong", e.strong), ("significant", e.significant)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"bucket edges {h}d: {name} must be a positive number")
        if not e.flat < e.significant < e.strong:
            raise ValueError(f"bucket edges {h}d: need flat < significant < strong (got {e.flat:g} / {e.significant:g} / {e.strong:g})")
        out[h] = Edges(float(e.flat), float(e.strong), float(e.significant))
    extra = {int(k) for k in table} - set(SUPPORTED_HORIZONS)
    if extra:
        raise ValueError(f"bucket edges: unsupported horizon(s) {sorted(extra)}")
    return out


# ── Lookup ────────────────────────────────────────────────────────────────────

_history_cache: dict[tuple[str, int], dict[int, Edges]] = {}


def _history_table(version: int) -> dict[int, Edges]:
    """The edges table that was current as `version` (immutable once written, so cached per database file)."""
    from portfolio_agent.tools import db as _db_module
    key = (str(_db_module.DB_PATH), version)
    if key not in _history_cache:
        from portfolio_agent.tools.engine_settings_db import get_edges_history
        table = get_edges_history(version)
        if table is None:
            raise ValueError(f"bucket edges version {version} is unknown")
        _history_cache[key] = table
    return _history_cache[key]


def clear_cache() -> None:
    _history_cache.clear()


def _pick(table: dict[int, Edges], horizon_days: int) -> Edges:
    """The edges for a horizon; a horizon between or beyond the supported ones uses the nearest one at or below it."""
    if horizon_days in table:
        return table[horizon_days]
    keys = sorted(table)
    below = [k for k in keys if k <= horizon_days]
    return table[below[-1] if below else keys[0]]


def edges_for(horizon_days: int | None, version: int | None = None, *, table: dict[int, Edges] | None = None) -> Edges:
    """
    The Edges for (horizon, version). Version 1 / None is the legacy fixed edges (no database read). A higher version comes
    from `table` if given, else from the immutable bucket_edges_history.
    """
    h = int(horizon_days) if horizon_days else 5
    v = LEGACY_VERSION if version is None or version != version else int(version)        # NaN (pandas) -> legacy
    if v == LEGACY_VERSION:
        return LEGACY_EDGES
    return _pick(table if table is not None else _history_table(v), h)


def canonical_version(horizon_days: int | None, version: int | None) -> int:
    """
    The lowest version whose edges for this horizon are IDENTICAL to `version`'s. Cohorts (rolling metrics, dashboards) group by
    this, so they never mix different edges but also never split a horizon whose edges did not change: the 5-day edges are the
    same in every version, so every 5-day row — the existing record included — stays one cohort.
    """
    v = LEGACY_VERSION if version is None or version != version else int(version)
    if v <= LEGACY_VERSION:
        return LEGACY_VERSION
    target = edges_for(horizon_days, v)
    for k in range(LEGACY_VERSION, v):
        try:
            if edges_for(horizon_days, k) == target:
                return k
        except ValueError:                     # a version number that was never used
            continue
    return v


def row_edges(row: dict) -> Edges:
    """The edges a prediction row was made under: its own horizon_days and bucket_edges_version (absent -> 5d, version 1,
    i.e. exactly the old behaviour)."""
    return edges_for(row.get("horizon_days"), row.get("bucket_edges_version"))


# ── Classification ────────────────────────────────────────────────────────────

def direction(ret: float, edges: Edges) -> str:
    return "UP" if ret > edges.flat else "DOWN" if ret < -edges.flat else "FLAT"


def bucket_of(ret: float, edges: Edges) -> str:
    if ret < -edges.strong:
        return "strong_down"
    if ret < -edges.flat:
        return "moderate_down"
    if ret < edges.flat:
        return "flat"
    if ret < edges.strong:
        return "moderate_up"
    return "strong_up"


def bounds(edges: Edges) -> list[tuple[str, float | None, float | None]]:
    """[(name, lower inclusive, upper exclusive)] in BUCKET_NAMES order; None = unbounded."""
    s, f = edges.strong, edges.flat
    return [("strong_down", None, -s), ("moderate_down", -s, -f), ("flat", -f, f), ("moderate_up", f, s), ("strong_up", s, None)]


# ── The prompt wording (rendered from the SAME edges the scorer uses) ─────────

def _pct(x: float) -> str:
    return f"{x * 100:.4f}".rstrip("0").rstrip(".")


def describe(edges: Edges) -> str:
    """'strong_down (<-5%), moderate_down (-5% to -1%), flat (-1% to +1%), moderate_up (+1% to +5%), strong_up (>+5%)'"""
    s, f = _pct(edges.strong), _pct(edges.flat)
    return (f"strong_down (<-{s}%), moderate_down (-{s}% to -{f}%), flat (-{f}% to +{f}%), "
            f"moderate_up (+{f}% to +{s}%), strong_up (>+{s}%)")


def parse_description(text: str) -> Edges | None:
    """Inverse of describe(): the (flat, strong) the text states, as Edges with significant 0 (not stated in the prompt).
    Used by the prompt/scorer consistency test to prove the two cannot drift."""
    import re
    m = re.search(r"strong_down \(<-([\d.]+)%\), moderate_down \(-([\d.]+)% to -([\d.]+)%\), flat \(-([\d.]+)% to \+([\d.]+)%\), "
                  r"moderate_up \(\+([\d.]+)% to \+([\d.]+)%\), strong_up \(>\+([\d.]+)%\)", text)
    if not m:
        return None
    g = [float(x) for x in m.groups()]
    strong, flat = g[0] / 100, g[3] / 100
    ok = (g[1] == g[0] and g[2] == g[3] and g[4] == g[3] and g[5] == g[3] and g[6] == g[0] and g[7] == g[0])
    return Edges(flat=flat, strong=strong, significant=0.0) if ok else None
