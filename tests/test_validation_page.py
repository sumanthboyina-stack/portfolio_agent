"""The user-facing Validation page imports and renders (empty DB and a small synthetic one) via Streamlit AppTest."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import portfolio_agent.tools.db as db_module

PAGE = "web/pages/6_🎯_Validation.py"


def _app():
    import runpy
    import sys
    from pathlib import Path
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import local_context
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_context = lambda source: local_context(source)
    runpy.run_path(str(root / "web/pages/6_🎯_Validation.py"), run_name="__main__")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    import web.data.validation as v
    monkeypatch.setattr(v, "_DB", db_path)
    v._price_cache.clear()
    from portfolio_agent.tools.prediction_db import _db
    with _db():          # creates the schema, as the running app has
        pass


def _insert_rows(n_dates=3, per_date=12):
    from portfolio_agent.tools.prediction_db import _db
    with _db() as conn:
        for d in range(n_dates):
            day = (date.today() - timedelta(days=10 + d)).isoformat()
            for i in range(per_date):
                conn.execute(
                    """INSERT INTO predictions (ticker, created_at, prediction, recommendation, horizon_days, as_of_date,
                           evaluation_date, evaluation_status, predicted_direction, actual_return, benchmark_return, excess_return,
                           composite_score, in_predicted_range, outcome, scope, model_name,
                           p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up, prediction_role)
                       VALUES (?, datetime('now'), 'x', ?, 5, ?, ?, 'evaluated', ?, ?, 0.0, ?, ?, ?, 'strong_correct', 'shared_market', 'm',
                           5, 10, 25, 40, 20, 'champion')""",
                    [f"T{d}_{i}", "BUY" if i >= per_date // 2 else "HOLD", day, day, "UP" if i >= per_date // 2 else "FLAT",
                     0.002 * i, 0.002 * i, float(i), i % 2])
        conn.commit()


def _run(at: AppTest) -> AppTest:
    at.run()
    return at


def test_page_renders_with_empty_data():
    at = _run(AppTest.from_function(_app, default_timeout=60))
    assert not at.exception, [e.value for e in at.exception]
    assert any("No scored predictions yet" in i.value for i in at.info)


def test_page_renders_four_cards_banner_and_charts_from_the_scorecard():
    _insert_rows()
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    for title in ("Do BUY picks win?", "Is the direction right?", "Are the chances good?", "Does it land in range?"):
        assert title in md
    assert md.count("Too early") >= 3 and "Info only" in md                      # 3 dates: below MIN_DATES_FOR_VERDICT
    assert any("Too early to judge" in w.value for w in at.warning)
    # old-page furniture is gone from the user page
    for gone in ("Baselines & sample size", "Headline: ranking skill", "FLAT diagnostic", "Follow every", "Outcome breakdown"):
        assert gone not in md and not any(gone in e.label for e in at.expander)
    assert [e.label for e in at.expander] == ["Prediction history"]


def test_every_card_number_is_the_scorecards_number():
    _insert_rows()
    from web.data.validation import _load_evaluated_predictions_df, build_scorecards
    from portfolio_agent.tools import validation_scorecard as scorecard_mod
    card = build_scorecards(_load_evaluated_predictions_df(segment="All"), use_prices=False)[5]
    at = _run(AppTest.from_function(_app, default_timeout=120))
    md = " ".join(m.value for m in at.markdown)
    for item in card.items:
        assert item.display in md and item.verdict in md


def _insert_other_horizon(horizon: int, n_dates: int, per_date: int = 4):
    from portfolio_agent.tools.prediction_db import _db
    with _db() as conn:
        for d in range(n_dates):
            day = (date.today() - timedelta(days=40 + d)).isoformat()
            for i in range(per_date):
                conn.execute(
                    """INSERT INTO predictions (ticker, created_at, prediction, recommendation, horizon_days, as_of_date,
                           evaluation_date, evaluation_status, predicted_direction, actual_return, benchmark_return, excess_return,
                           composite_score, in_predicted_range, outcome, scope, model_name, prediction_role)
                       VALUES (?, datetime('now'), 'x', 'BUY', ?, ?, ?, 'evaluated', 'UP', 0.01, 0.0, 0.01, 5.0, 1,
                           'strong_correct', 'shared_market', 'm', 'champion')""",
                    [f"H{horizon}_{d}_{i}", horizon, day, day])
        conn.commit()


def test_page_has_no_filters_and_nothing_lives_in_the_sidebar():
    _insert_rows(n_dates=12, per_date=4)
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    assert not at.sidebar.children or len(at.sidebar.children) == 0
    assert not at.selectbox and not at.multiselect
    assert [r.label for r in at.radio] == []                                           # no Scope / Horizon selector
    assert {t.label for t in at.toggle} == {"Wrong calls only"}                        # only the history table's own toggle


def test_every_horizon_is_a_column_of_the_grid_and_the_headline_names_the_best_one():
    _insert_rows(n_dates=12)
    _insert_other_horizon(21, n_dates=11)
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    assert "5-Day" in md and "21-Day" in md
    for row in ("Do BUY picks win?", "Is the direction right?", "Are the chances good?", "Does it land in range?"):
        assert row in md
    banner = " ".join(e.value for e in list(at.info) + list(at.success) + list(at.warning))
    assert "5-Day calls" in banner and "days of results" in banner


def test_a_horizon_with_few_dates_is_still_shown_in_the_grid_as_too_early():
    _insert_rows(n_dates=12)
    _insert_other_horizon(21, n_dates=1)
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    assert "21-Day" in md and "Too early" in md


def test_wrong_calls_only_toggle_filters_the_history_table():
    _insert_rows(n_dates=12)
    from portfolio_agent.tools.prediction_db import _db
    with _db() as conn:
        conn.execute("UPDATE predictions SET outcome = 'wrong_minor' WHERE ticker LIKE 'T0_%' AND recommendation = 'HOLD'")
        conn.commit()
    at = _run(AppTest.from_function(_app, default_timeout=120))
    n_all = int(next(c.value.split(" of ")[0] for c in at.caption if " evaluated " in c.value and " shown" in c.value))
    at.toggle(key="pred_tab_wrong").set_value(True).run()
    assert not at.exception, [e.value for e in at.exception]
    n_wrong = int(next(c.value.split(" of ")[0] for c in at.caption if " evaluated " in c.value and " shown" in c.value))
    assert 0 < n_wrong < n_all
