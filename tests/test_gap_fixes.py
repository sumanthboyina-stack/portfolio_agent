"""
Follow-ups from the Step 2–3 review: the prediction-save lock contention, goal names in the privacy filter,
account-level Portfolio Health notifications, and the horizons change + its audit event in ONE transaction.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent import privacy
from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services import account_service, health_service as hs, investor_service as inv
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import auth_users_db, engine_settings_db as esdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_profile_db as pdb
from portfolio_agent.tools import portfolio_health as ph
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import prediction_db as predb

ME = local_context("test")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})


# ── Lock contention: setup must not take the write lock when there is nothing to migrate ──

def test_opening_the_predictions_db_does_not_need_the_write_lock_when_nothing_is_left_to_migrate():
    predb.get_today_horizons("X", "2000-01-01")                      # first touch creates and migrates
    holder = sqlite3.connect(str(db_module.DB_PATH), timeout=1)
    holder.execute("BEGIN IMMEDIATE")                                # another writer holds the lock
    result = {}

    def reader():
        start = time.monotonic()
        result["horizons"] = predb.get_today_horizons("X", "2000-01-01")
        result["seconds"] = time.monotonic() - start
    t = threading.Thread(target=reader)
    t.start()
    t.join(timeout=8)
    holder.rollback(); holder.close()
    assert not t.is_alive() and result["horizons"] == set()
    assert result["seconds"] < 2.0                                    # a bare UPDATE here used to wait out the 10s busy timeout


def test_legacy_rows_are_still_backfilled_by_the_next_connection():
    predb.get_today_horizons("X", "2000-01-01")
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("INSERT INTO predictions (ticker, as_of_date, horizon_days, prediction, recommendation, created_at) VALUES ('OLD', '2026-01-02', 5, 'UP', 'BUY', '2026-01-02')")
    predb.get_today_horizons("X", "2000-01-01")
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        row = c.execute("SELECT prediction_role, scope, owner_scope FROM predictions WHERE ticker = 'OLD'").fetchone()
    assert row == (predb.ROLE_LEGACY_RANDOM, predb.SCOPE_LEGACY, f"user:{LOCAL_OWNER}")


def test_a_failed_setup_closes_its_connection_instead_of_leaking_a_write_lock():
    def bad_setup(conn):
        conn.execute("CREATE TABLE leak_probe (x)")                   # takes the write lock...
        raise RuntimeError("setup failed")                            # ...then fails
    with pytest.raises(RuntimeError):
        with db_module.db_conn(setup=bad_setup):
            pass
    other = sqlite3.connect(str(db_module.DB_PATH), timeout=0.5)
    other.execute("BEGIN IMMEDIATE")                                  # would raise "database is locked" if the lock leaked
    other.rollback(); other.close()


# ── Goal names are private terms ──────────────────────────────────────────────

def test_goal_names_are_private_terms_but_a_bare_goal_type_word_is_not():
    pdb.add_goal(LOCAL_OWNER, goal_type="EDUCATION", name="Emma's college fund")
    pdb.add_goal(LOCAL_OWNER, goal_type="RETIREMENT", name="Retirement")
    pdb.add_goal(LOCAL_OWNER, goal_type="OTHER", name="Tiny")
    terms = privacy.collect_private_terms()
    assert "Emma's college fund" in terms
    assert "Retirement" not in terms and "Tiny" not in terms         # an ordinary word / too short to be a private phrase
    assert privacy.find_private_terms("news about Emma's college fund today", terms) == ["Emma's college fund"]
    with pytest.raises(privacy.PrivateDataLeak):
        privacy.assert_market_only("saving for emma's COLLEGE fund", terms)


# ── Account-level health notifications ────────────────────────────────────────

def _acct(name):
    return account_service.create_account(ME, broker="manual", display_name=name, account_type="TAXABLE")


def _hold(acct, ticker, value):
    account_service.add_position(ME, acct["account_id"], {"ticker": ticker, "shares": value / 100.0, "current_price": 100.0,
                                                          "sector": "Technology", "price_as_of": "2026-10-08"})


def _queued():
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        try:
            return sorted(r[0] for r in c.execute("SELECT dedup_key FROM notification_outbox WHERE dedup_key LIKE 'health:%' "
                                                  "AND status IN ('PENDING', 'DEFERRED', 'SENDING', 'SENT')"))
        except sqlite3.OperationalError:
            return []


def test_an_action_inside_one_account_notifies_once_with_its_own_key():
    from portfolio_agent.services import notification_service as nsvc
    from portfolio_agent.tools import notification_settings_db as nsdb
    # (rewritten in the notifications task: alerts go through notify() into the outbox)
    auth_users_db.invite_user("owner@example.com", LOCAL_OWNER, "member")
    nsdb.update_notification_settings(updated_by="t", email_from_address="apex@example.com", max_messages_per_user_per_hour=1000)
    nsvc.ensure_contacts(ME)
    big, spread = _acct("Concentrated"), _acct("Spread")
    _hold(big, "ONLY1", 5000.0)                                         # one security: diversification ACTION inside this account
    for i in range(10):
        _hold(spread, f"OK{i:02d}", 1000.0)
    p = repo.get_portfolio_by_owner(LOCAL_OWNER)
    ph.clear_health_cache()
    health = ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, use_cache=False)
    scope = f"account:{big['account_id']}"
    assert health.by_account[str(big["account_id"])].check("diversification").status == "ACTION"
    sent = hs._notify_new_actions(LOCAL_OWNER, health, date(2026, 10, 9))
    assert f"health:{scope}:diversification:2026-10-09" in sent
    assert not any(f"account:{spread['account_id']}:diversification" in k for k in sent)           # the spread account is fine
    assert hs._notify_new_actions(LOCAL_OWNER, health, date(2026, 10, 9)) == []          # same run: no repeat
    assert all(k.startswith("health:") for k in sent) and len(sent) == len(set(sent))
    assert f"health:{scope}:diversification:2026-10-09" in _queued()
    row = [r for r in nsvc.obx.list_recent(LOCAL_OWNER) if r["dedup_key"] == f"health:{scope}:diversification:2026-10-09"][0]
    assert "Concentrated" not in row["body_brief"] and "one of your accounts" in row["body_brief"]     # no account names in the brief


# ── Horizons change and its audit event are one transaction ───────────────────

def test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither():
    auth_users_db.invite_user("me@example.com", LOCAL_OWNER, "admin")
    version = esdb.get_engine_settings()["version"]
    inv.update_instrument_type_horizons(ME, {"ETF": [21]}, expected_version=version, reason="t")
    ops = [e["operation"] for e in repo.get_audit_events(limit=50)]
    assert ops.count("update_instrument_type_horizons") == 1 and esdb.get_engine_settings()["instrument_type_horizons"]["ETF"] == [21]
    with pytest.raises(RuntimeError):                                    # stale expected_version
        inv.update_instrument_type_horizons(ME, {"ETF": [63]}, expected_version=version)
    assert esdb.get_engine_settings()["instrument_type_horizons"]["ETF"] == [21]
    assert [e["operation"] for e in repo.get_audit_events(limit=50)].count("update_instrument_type_horizons") == 1
    with pytest.raises(ValueError):                                      # invalid input: no change, no audit event
        inv.update_instrument_type_horizons(ME, {"ETF": [7]})
    assert [e["operation"] for e in repo.get_audit_events(limit=50)].count("update_instrument_type_horizons") == 1
    ev = [e for e in repo.get_audit_events(limit=50) if e["operation"] == "update_instrument_type_horizons"][0]
    assert ev["before"]["ETF"] == [5, 21, 63, 250] and ev["after"]["ETF"] == [21]
