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
    at.session_state["val_segment"] = "All"
    at.run()
    return at


def test_page_renders_with_empty_data():
    at = _run(AppTest.from_function(_app, default_timeout=60))
    assert not at.exception, [e.value for e in at.exception]
    assert any("No evaluated predictions yet" in i.value for i in at.info)


def test_page_renders_four_cards_banner_and_charts_from_the_scorecard():
    _insert_rows()
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    for title in ("Does the rating pick winners?", "Is the call direction right?", "Are the stated chances any good?",
                  "Does the return land in the stated range?"):
        assert title in md
    assert md.count("Too early") >= 3 and "Info only" in md                      # 3 dates: below MIN_DATES_FOR_VERDICT
    assert any("evidence: Too early" in w.value for w in at.warning)
    # old-page furniture is gone from the user page
    for gone in ("Baselines & sample size", "Headline: ranking skill", "FLAT diagnostic", "Follow every", "Outcome breakdown"):
        assert gone not in md and not any(gone in e.label for e in at.expander)
    assert [e.label for e in at.expander] == ["Prediction history"]


def test_every_card_number_is_the_scorecards_number():
    _insert_rows()
    from web.data.validation import _load_evaluated_predictions_df, build_scorecards
    card = build_scorecards(_load_evaluated_predictions_df(segment="All"), use_prices=False)[5]
    at = _run(AppTest.from_function(_app, default_timeout=120))
    md = " ".join(m.value for m in at.markdown)
    for item in card.items:
        assert item.display in md and item.one_line_reason in md


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


def test_only_two_controls_are_visible_and_nothing_lives_in_the_sidebar():
    _insert_rows(n_dates=12, per_date=4)       # <= 60 tickers, so the price-baselines toggle isn't needed
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    assert [r.label for r in at.radio] == ["Scope", "Horizon"]
    assert at.radio(key="val_segment").options == ["My tickers", "Everything APEX predicted"]
    assert not at.sidebar.children or len(at.sidebar.children) == 0
    assert [s.label for s in at.selectbox] == ["Lookback period"]                       # inside the Advanced popover
    assert {t.label for t in at.toggle} == {"Include legacy predictions", "Wrong calls only"}
    assert not at.multiselect


def test_horizons_without_enough_dates_are_left_out_of_the_selector_and_named_in_a_note():
    _insert_rows(n_dates=12)                  # 5d: 12 dates -> ready
    _insert_other_horizon(21, n_dates=1)      # 21d: 1 date -> too early
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio(key="val_horizon").options == ["5-Day"]
    assert any("21-Day (1 date)" in c.value and "Not shown yet" in c.value for c in at.caption)


def test_when_no_horizon_is_ready_every_horizon_is_still_offered_as_a_preview():
    _insert_rows(n_dates=3)
    _insert_other_horizon(21, n_dates=1)
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio(key="val_horizon").options == ["5-Day", "21-Day"]
    assert at.radio(key="val_horizon").value == 5                                  # the one with more evidence


def test_the_horizon_with_the_most_dates_is_the_default_and_the_banner_is_the_headline():
    _insert_rows(n_dates=12)
    _insert_other_horizon(21, n_dates=11)
    at = _run(AppTest.from_function(_app, default_timeout=120))
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio(key="val_horizon").options == ["5-Day", "21-Day"]
    banner = " ".join(e.value for e in list(at.info) + list(at.success) + list(at.warning))
    assert "5-Day calls" in banner and "checks:" in banner


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
