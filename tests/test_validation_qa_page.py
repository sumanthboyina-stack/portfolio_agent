"""Validation QA (admin) imports and renders all of its tabs -- empty DB and a small synthetic one -- via AppTest."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

import portfolio_agent.tools.db as db_module

NEW_TABS = ["Baselines", "Ranking detail", "Calibration", "Models", "FLAT diagnostic", "Legacy tiles & outcomes",
            "Equity curve", "Scoring snapshots"]
OLD_TABS = ["Metric Trends", "Confidence Calibration", "Post-mortems", "Versions"]


def _app():
    import runpy
    import sys
    import types
    from pathlib import Path
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import local_context
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_user = lambda: types.SimpleNamespace(is_admin=True)
    auth.current_context = lambda source: local_context(source)
    runpy.run_path(str(root / "web/pages/11_🔬_Validation_QA.py"), run_name="__main__")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    import web.data.validation as v
    monkeypatch.setattr(v, "_DB", db_path)
    v._price_cache.clear()
    from portfolio_agent.tools.prediction_db import _db
    with _db():
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
                           composite_score, in_predicted_range, outcome, scope, model_name, brier_score, log_loss,
                           p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up, prediction_role)
                       VALUES (?, datetime('now'), 'x', ?, 5, ?, ?, 'evaluated', ?, ?, 0.0, ?, ?, ?, 'strong_correct', 'shared_market', 'm',
                           0.2, 0.6, 5, 10, 25, 40, 20, 'champion')""",
                    [f"T{d}_{i}", "BUY" if i >= per_date // 2 else "HOLD", day, day, "UP" if i >= per_date // 2 else "FLAT",
                     0.002 * i, 0.002 * i, float(i), i % 2])
        conn.commit()


def _run() -> AppTest:
    at = AppTest.from_function(_app, default_timeout=120)
    at.session_state["valqa_segment"] = "All"
    at.run()
    return at


def test_qa_page_renders_all_tabs_with_empty_data():
    at = _run()
    assert not at.exception, [e.value for e in at.exception]
    labels = " ".join(t.label for t in at.tabs)
    for name in OLD_TABS + NEW_TABS:
        assert name in labels, name
    assert len(at.tabs) == 14                          # + Roles (champion / fallback / challenger) + Instrument types


def test_qa_page_hosts_the_components_removed_from_the_user_page():
    _insert_rows()
    at = _run()
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown) + " " + " ".join(i.value for i in at.info) + " " + " ".join(c.value for c in at.caption)
    assert "Headline: ranking skill" in md                          # legacy tile grid
    assert "compounded call-return index, not a portfolio" in md    # equity-curve label
    assert "Effective sample" in md                                 # baselines tab
    assert "Mean excess return by APEX label" in md                 # ranking detail
    assert "Probability scores against the sample" in md            # calibration tab
