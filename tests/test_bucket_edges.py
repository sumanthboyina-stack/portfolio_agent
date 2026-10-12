"""Horizon-scaled return buckets (Step 1.1): per-horizon edges, versions, prompt/scorer consistency, never mixing versions."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.pipeline.daily.apex import _build_horizons_instruction
from portfolio_agent.services import investor_service as svc
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import bucket_edges as be
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import validation_metrics as vm
from portfolio_agent.tools.validation_engine import CORRECT_OUTCOMES, score_outcome

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    be.clear_cache()
    yield
    be.clear_cache()


def _admin():
    ctx = local_context("test")
    auth_users_db.invite_user(f"{ctx.actor}@example.com", ctx.actor, "admin")
    return ctx


def _row(h, ret, version, direction):
    return {"horizon_days": h, "bucket_edges_version": version, "predicted_direction": direction}


# ── edges per horizon / version ───────────────────────────────────────────────

def test_seed_edges_scale_with_the_horizon_and_5d_equals_legacy():
    v, table = esdb.current_bucket_edges()
    assert v == be.SEED_VERSION
    assert table[5] == be.LEGACY_EDGES
    flats = [table[h].flat for h in (5, 21, 63, 250)]
    assert flats == sorted(flats) and len(set(flats)) == 4


def test_flat_is_achievable_at_250d_and_not_at_legacy_edges():
    # +6% over a year is "flat" under the 250d edges (8%), but UP under the legacy ±1%
    assert be.direction(0.06, be.edges_for(250, be.SEED_VERSION)) == "FLAT"
    assert be.direction(0.06, be.edges_for(250, 1)) == "UP"


def test_legacy_rows_are_scored_with_version_1_and_new_rows_with_their_version():
    # +6% over 250 days: under the legacy edges (±1%) an UP call is right; under the 250d edges (±8%) it was a FLAT move
    legacy_up = score_outcome(_row(250, 0.06, None, "UP"), 0.06)
    new_up = score_outcome(_row(250, 0.06, be.SEED_VERSION, "UP"), 0.06)
    new_flat = score_outcome(_row(250, 0.06, be.SEED_VERSION, "FLAT"), 0.06)
    assert legacy_up.label in CORRECT_OUTCOMES
    assert new_up.label not in CORRECT_OUTCOMES
    assert new_flat.label in CORRECT_OUTCOMES


def test_5d_results_do_not_move_between_versions():
    for ret in (-0.07, -0.03, 0.0, 0.0099, 0.01, 0.03, 0.05, 0.2):
        assert be.bucket_of(ret, be.edges_for(5, 1)) == be.bucket_of(ret, be.edges_for(5, be.SEED_VERSION))
    assert be.canonical_version(5, be.SEED_VERSION) == 1
    assert be.canonical_version(250, be.SEED_VERSION) == be.SEED_VERSION


def test_changing_edges_does_not_rescore_old_rows():
    ctx = _admin()
    row = _row(21, 0.03, be.SEED_VERSION, "UP")
    before = be.row_edges(row)
    after = svc.update_horizon_bucket_edges(ctx, {21: {"flat": 0.03, "significant": 0.05, "strong": 0.09}},
                                            expected_version=esdb.get_engine_settings()["version"])
    assert after["version"] == be.SEED_VERSION + 1
    assert be.row_edges(row) == before                    # the row keeps the edges it was made under
    assert be.edges_for(21, after["version"]).flat == 0.03


def test_unchanged_save_does_not_bump_the_version_and_stale_version_is_refused():
    ctx = _admin()
    es = esdb.get_engine_settings()
    same = {h: e.to_dict() for h, e in es["horizon_bucket_edges"].items()}
    assert svc.update_horizon_bucket_edges(ctx, same, expected_version=es["version"])["version"] == be.SEED_VERSION
    svc.update_horizon_bucket_edges(ctx, {63: {"flat": 0.05, "significant": 0.08, "strong": 0.13}}, expected_version=es["version"])
    with pytest.raises(Exception):
        svc.update_horizon_bucket_edges(ctx, {63: {"flat": 0.04, "significant": 0.07, "strong": 0.12}}, expected_version=es["version"])


def test_edges_validation_and_admin_only_and_reset():
    ctx = _admin()
    with pytest.raises(ValueError):
        svc.update_horizon_bucket_edges(ctx, {21: {"flat": 0.05, "significant": 0.04, "strong": 0.08}})
    stranger = RequestContext(identity=_mint_identity_for_tests("nobody"), source="test")
    with pytest.raises(Exception):
        svc.update_horizon_bucket_edges(stranger, {21: {"flat": 0.02, "significant": 0.04, "strong": 0.08}})
    svc.update_horizon_bucket_edges(ctx, {21: {"flat": 0.03, "significant": 0.05, "strong": 0.09}})
    out = svc.reset_horizon_bucket_edges(ctx)
    assert out["edges"][21] == be.SEED_EDGES[21].to_dict()
    hist = esdb.get_edges_history(be.SEED_VERSION)
    assert hist[21] == be.SEED_EDGES[21]                 # history is append-only: the seed version is still intact


# ── prompt / scorer consistency ───────────────────────────────────────────────

def test_prompt_ranges_are_exactly_the_scorers_edges():
    _, table = esdb.current_bucket_edges()
    text = _build_horizons_instruction([5, 21, 63, 250])
    found = {}
    for line in text.splitlines():
        m = re.search(r"return buckets for the (\d+)d distribution: (.*)$", line)
        if m:
            found[int(m.group(1))] = be.parse_description(m.group(2))
    assert set(found) == {5, 21, 63, 250}
    for h, e in found.items():
        assert e is not None
        assert (e.flat, e.strong) == (table[h].flat, table[h].strong)


# ── never mix versions in one metric ──────────────────────────────────────────

def test_a_cohort_mixing_different_edges_raises():
    df = pd.DataFrame({"horizon_days": [250, 250], "bucket_edges_version": [1, be.SEED_VERSION]})
    with pytest.raises(ValueError):
        vm._edges_of(df, 250)
    same = pd.DataFrame({"horizon_days": [5, 5], "bucket_edges_version": [1, be.SEED_VERSION]})
    assert vm._edges_of(same, 5) == be.LEGACY_EDGES


# ── migration idempotence ─────────────────────────────────────────────────────

def test_migration_is_idempotent_and_keeps_history():
    esdb.get_engine_settings()
    for _ in range(3):
        esdb.get_engine_settings()
    with sqlite3.connect(db_module.DB_PATH) as c:
        n = c.execute("SELECT COUNT(*) FROM bucket_edges_history").fetchone()[0]
    assert n >= 2                                        # v1 (legacy) and the seed version, written once


# ── nothing scores with the old fixed constants ───────────────────────────────

def test_no_scoring_code_uses_the_fixed_flat_threshold():
    allowed = {"portfolio_agent/tools/bucket_edges.py", "portfolio_agent/tools/validation_engine.py",
               "portfolio_agent/tools/validation_metrics.py"}
    offenders = []
    for p in list((ROOT / "portfolio_agent").rglob("*.py")) + list((ROOT / "web").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        if "FLAT_THRESHOLD" in p.read_text():
            offenders.append(rel)
    assert offenders == []
