"""One-off: remove the test debris that a mis-isolated test run wrote into the live DB on 2026-10-11 ~16:14 (and only that).

Run from the repo root:   .venv/bin/python scripts/cleanup_test_debris_2026-10-13.py

It takes a backup first, then deletes in ONE transaction with every count asserted; any mismatch rolls everything back and changes nothing.
(AAA and BBB were already removed on 2026-10-13; backup data/backups/portfolio.db.before-AAA-BBB-removal-2026-10-13.)

Deletes:
  accounts 6-13 (the 8 test accounts, created 2026-10-11 16:14) with their 3 positions (incl. a fake 900-share VOO and SPAXX / FZFXX) and 6 cash rows
  19 audit events with source='test' (ids 21-39)
  84 portfolio_price_history rows the evening snapshot wrote for those test accounts (dates >= 2026-10-11)
  instruments SPAXX (52) and FZFXX (54), portfolios 'second' (2785) and 'someone-else' (2776)

Does NOT touch: the real VOO instrument (it was flipped to manual/ETF by the test: re-classify it from the Accounts page), or portfolio_settings
version 2 (the test edited the health thresholds: reset them under Profile -> Admin -> Fund data -> "Reset all portfolio settings" if you want the defaults).
"""
import shutil
import sqlite3
from datetime import date

LIVE = "data/portfolio.db"
BACKUP = f"data/backups/portfolio.db.before-debris-cleanup-{date.today().isoformat()}"
shutil.copy(LIVE, BACKUP)
print("backup:", BACKUP)

c = sqlite3.connect(LIVE, timeout=60)
c.execute("BEGIN IMMEDIATE")
T = (6, 7, 8, 9, 10, 11, 12, 13)
q_ = ",".join("?" * len(T))


def one(sql, *a):
    return c.execute(sql, a).fetchone()[0]


try:
    assert one(f"select count(*) from accounts where account_id in ({q_}) and created_at >= '2026-10-11 16:14'", *T) == 8
    assert one(f"select count(*) from accounts where account_id in ({q_}) and (broker, account_number) in "
               f"(select broker, account_number from accounts where account_id not in ({q_}))", *T, *T) == 0
    assert one(f"select count(*) from positions where account_id in ({q_})", *T) == 3
    assert one(f"select count(*) from account_cash where account_id in ({q_})", *T) == 6
    assert one("select count(*) from audit_events where source='test'") == 19
    assert one("select count(*) from audit_events where source='test' and event_id between 21 and 39") == 19
    assert one(f"select count(*) from audit_events where account_id in ({q_}) and source != 'test'", *T) == 0
    ph = f"exists (select 1 from accounts a where a.account_id in ({q_}) and a.broker = ph.broker and a.account_number = ph.account_number)"
    assert one(f"select count(*) from portfolio_price_history ph where {ph}", *T) == 84
    assert one(f"select count(*) from portfolio_price_history ph where {ph} and ph.date < '2026-10-11'", *T) == 0

    def delete(label, sql, expected, *a):
        n = c.execute(sql, a).rowcount
        assert n == expected, f"{label}: deleted {n}, expected {expected}"
        print(f"deleted {n:3} {label}")

    delete("portfolio_price_history", f"delete from portfolio_price_history where rowid in (select ph.rowid from portfolio_price_history ph where {ph})", 84, *T)
    delete("audit_events (source=test)", "delete from audit_events where source='test' and event_id between 21 and 39", 19)
    delete("positions", f"delete from positions where account_id in ({q_})", 3, *T)
    delete("account_cash", f"delete from account_cash where account_id in ({q_})", 6, *T)
    delete("accounts", f"delete from accounts where account_id in ({q_})", 8, *T)
    delete("instruments SPAXX / FZFXX", "delete from instruments where instrument_id in (52, 54) and ticker in ('SPAXX', 'FZFXX') "
           "and instrument_id not in (select instrument_id from positions)", 2)
    delete("portfolios second / someone-else", "delete from portfolios where portfolio_id in (2776, 2785) and owner in ('someone-else', 'second') "
           "and portfolio_id not in (select portfolio_id from accounts)", 2)
    c.execute("COMMIT")
    print("committed")
except BaseException:
    c.execute("ROLLBACK")
    print("ROLLED BACK - nothing was changed")
    raise
