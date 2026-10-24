from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tattle import db, isp
from tattle.api import create_app

CAIRO = ZoneInfo("Africa/Cairo")
GB = 1024**3


def at(day: dt.date, hour: int = 12) -> float:
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=CAIRO).timestamp()


def plan_item(total: int, remaining: int, renews: str | None) -> dict:
    return {"kind": "plan", "is_data": True, "total_bytes": total, "remaining_bytes": remaining, "renews": renews}


def extra_item(total: int | None, remaining: int) -> dict:
    return {"kind": "extra", "is_data": True, "total_bytes": total, "remaining_bytes": remaining, "renews": None}


@pytest.fixture
def conn(settings):
    db.init(settings.db_path)
    connection = db.connect(settings.db_path)
    connection.execute(
        "INSERT INTO provider_accounts(id, provider, identifier, credentials, status, created, updated) "
        "VALUES(1, 'eand_eg', '0221234567', '{}', 'ok', 0, 0)"
    )
    for number in ("0221234567", "0221234568"):
        connection.execute(
            "INSERT INTO isp_lines(number, login_id, provider, plan) VALUES(?, 1, 'eand_eg', '750 GB')", (number,)
        )
    yield connection
    connection.close()


def reading(conn, number: str, when: float, renews: str | None, items: list[dict]) -> None:
    conn.execute(
        "INSERT INTO isp_readings(number, ts, total, remaining, renews, live, counted, items) VALUES(?, ?, 0, 0, ?, 0, 0, ?)",
        (number, int(when), renews, json.dumps(items)),
    )


def test_cycle_follows_the_live_number_from_its_last_renewal(conn):
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234568")
    reading(conn, "0221234568", at(dt.date(2026, 10, 10)), "15/10/2026", [plan_item(750 * GB, 10 * GB, "15/10/2026")])
    reading(conn, "0221234568", at(dt.date(2026, 10, 20)), "15/11/2026", [plan_item(750 * GB, 600 * GB, "15/11/2026")])
    # The other number renews on another day; it is not the one in use.
    reading(conn, "0221234567", at(dt.date(2026, 10, 20)), "25/10/2026", [plan_item(750 * GB, 0, "25/10/2026")])

    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 24)))
    assert (cycle["number"], cycle["start"], cycle["end"]) == ("0221234568", dt.date(2026, 10, 15), dt.date(2026, 11, 15))
    assert (cycle["used"], cycle["remaining"], cycle["bytes"], cycle["pending"]) == (150 * GB, 600 * GB, 750 * GB, False)

    # The router switches to the other number: the cycle is that number's.
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 24)))
    assert (cycle["number"], cycle["start"], cycle["end"]) == ("0221234567", dt.date(2026, 9, 25), dt.date(2026, 10, 25))


def test_cycle_on_renewal_day_before_e_and_shows_the_new_date_is_pending(conn):
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    reading(conn, "0221234567", at(dt.date(2026, 10, 24)), "25/10/2026", [plan_item(750 * GB, 0, "25/10/2026")])

    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 25), hour=1))
    assert (cycle["start"], cycle["end"], cycle["pending"]) == (dt.date(2026, 10, 25), dt.date(2026, 11, 25), True)
    assert cycle["remaining"] is None and cycle["used"] is None  # e&'s figures are still last cycle's

    reading(conn, "0221234567", at(dt.date(2026, 10, 25), hour=2), "25/11/2026", [plan_item(750 * GB, 750 * GB, "25/11/2026")])
    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 25), hour=3))
    assert (cycle["start"], cycle["end"], cycle["pending"]) == (dt.date(2026, 10, 25), dt.date(2026, 11, 25), False)
    assert cycle["remaining"] == 750 * GB


def test_cycle_renewing_on_the_31st_does_not_drift(conn):
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    reading(conn, "0221234567", at(dt.date(2026, 10, 1)), "31/10/2026", [plan_item(750 * GB, 5 * GB, "31/10/2026")])
    reading(conn, "0221234567", at(dt.date(2026, 11, 1)), "30/11/2026", [plan_item(750 * GB, 700 * GB, "30/11/2026")])
    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 11, 2)))
    assert (cycle["start"], cycle["end"]) == (dt.date(2026, 10, 31), dt.date(2026, 11, 30))

    # Without an earlier date, the start is a month before the renewal (clamped).
    conn.execute("DELETE FROM isp_readings WHERE renews = '31/10/2026'")
    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 11, 2)))
    assert cycle["start"] == dt.date(2026, 10, 30)


def test_cycle_counts_extra_bundles_and_skips_readings_without_a_date(conn):
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    items = [plan_item(750 * GB, 100 * GB, "15/11/2026"), extra_item(10 * GB, 4 * GB), extra_item(None, 3 * GB)]
    reading(conn, "0221234567", at(dt.date(2026, 10, 20)), "15/11/2026", items)
    reading(conn, "0221234567", at(dt.date(2026, 10, 21)), None, [])
    cycle = isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 22)))
    # Used is counted only where e& gives a size; what is left includes every bundle.
    assert cycle["used"] == 650 * GB + 6 * GB
    assert cycle["remaining"] == 107 * GB
    assert cycle["plan_bytes"] == 750 * GB
    assert cycle["bytes"] == cycle["used"] + cycle["remaining"]


def test_cycle_without_a_router_uses_the_number_e_and_charged_last(conn):
    reading(conn, "0221234567", at(dt.date(2026, 10, 20)), "25/10/2026", [plan_item(750 * GB, 0, "25/10/2026")])
    reading(conn, "0221234568", at(dt.date(2026, 10, 20)), "15/11/2026", [plan_item(750 * GB, 600 * GB, "15/11/2026")])
    conn.execute("INSERT INTO isp_daily(number, day, bytes) VALUES('0221234567', '2026-10-12', 5), ('0221234568', '2026-10-19', 5)")
    assert isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 22)))["number"] == "0221234568"


def test_no_renewal_date_means_no_e_and_cycle(conn):
    assert isp.current_cycle(conn, CAIRO, now=at(dt.date(2026, 10, 22))) is None


def test_overview_and_settings_take_the_cycle_from_e_and(settings, conn):
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234568")
    db.set_setting(conn, "billing_day", "1")
    today = dt.datetime.now(CAIRO).date()
    start, renews = today - dt.timedelta(days=3), today + dt.timedelta(days=27)
    reading(conn, "0221234568", at(start - dt.timedelta(days=2)), start.strftime("%d/%m/%Y"), [plan_item(750 * GB, GB, None)])
    reading(conn, "0221234568", at(today) - 13 * 3600, renews.strftime("%d/%m/%Y"), [plan_item(750 * GB, 700 * GB, None)])

    app = create_app(settings, start_collector=False)
    with TestClient(app) as client:
        settings_body = client.get("/api/settings").json()
        assert settings_body["cycle"]["source"] == "isp"
        assert settings_body["cycle"]["start"] == start.isoformat()
        overview = client.get("/api/overview?range=cycle").json()
        assert overview["period"]["label"] == f"Since {start:%b} {start.day}"
        quota = overview["quota"]
        assert quota["source"] == "isp" and quota["used"] == 50 * GB and quota["bytes"] == 750 * GB
        assert quota["cycle_end"] == dt.datetime(renews.year, renews.month, renews.day, tzinfo=CAIRO).timestamp()

        # The typed day is not needed any more, and saving the plan without it works.
        assert client.post("/api/auth/login", json={"password": "s3cret"}).status_code == 200
        assert client.put("/api/settings", json={"quota_gb": None}).status_code == 200
        assert db.get_setting(conn, "billing_day") == "1"
