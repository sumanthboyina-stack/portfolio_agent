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
    assert any("Evidence: Too early" in w.value for w in at.warning)
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
