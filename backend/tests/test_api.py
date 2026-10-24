from __future__ import annotations

from helpers import host_clock

import pytest
from fastapi.testclient import TestClient

from tattle.api import create_app


@pytest.fixture
def client(settings, host):
    app = create_app(settings, start_collector=False)
    with TestClient(app) as c:
        yield c


def seeded_day(settings, host) -> str:
    """The day the fake host wrote its counters on (not "today": near midnight they differ)."""
    import datetime as dt

    day = dt.datetime.fromtimestamp(host.clock, settings.timezone).date()
    return f"range=custom&from={day}&to={day}"


def seed(settings, host):
    from tattle import db
    from tattle.collector import Collector, LiveState

    host.write_leases(
        ("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone", None),
        ("aa:aa:aa:aa:aa:02", "192.168.1.51", "laptop", None),
    )
    db.init(settings.db_path)
    collector = Collector(settings, LiveState(), clock=host_clock(settings))
    host.write_counters(up={"192.168.1.50": 10}, down={"192.168.1.50": 1_000, "192.168.1.51": 400}, server=(5, 50))
    collector.tick()


def test_overview_totals_and_ranking(client, settings, host):
    seed(settings, host)
    data = client.get(f"/api/overview?{seeded_day(settings, host)}").json()
    assert data["totals"] == {"rx": 1_450, "tx": 15}
    assert [d["name"] for d in data["devices"]] == ["phone", "laptop", "Tattle (this server)"]


def test_devices_lists_idle_and_active(client, settings, host):
    seed(settings, host)
    devices = client.get(f"/api/devices?{seeded_day(settings, host)}").json()["devices"]
    phone = next(d for d in devices if d["name"] == "phone")
    assert (phone["rx"], phone["tx"], phone["is_new"]) == (1_000, 10, True)


def test_rename_requires_admin(client, settings, host):
    seed(settings, host)
    phone = next(d for d in client.get("/api/devices").json()["devices"] if d["name"] == "phone")
    assert client.patch(f"/api/devices/{phone['id']}", json={"name": "Mum's phone"}).status_code == 401

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "s3cret"}).status_code == 200
    assert client.patch(f"/api/devices/{phone['id']}", json={"name": "Mum's phone"}).status_code == 200
    names = [d["name"] for d in client.get("/api/devices").json()["devices"]]
    assert "Mum's phone" in names


def test_merge_moves_history(client, settings, host):
    seed(settings, host)
    devices = {d["name"]: d for d in client.get("/api/devices").json()["devices"]}
    client.post("/api/auth/login", json={"password": "s3cret"})
    r = client.post(f"/api/devices/{devices['laptop']['id']}/merge", json={"into": devices["phone"]["id"]})
    assert r.status_code == 200
    after = {d["name"]: d for d in client.get(f"/api/devices?{seeded_day(settings, host)}").json()["devices"]}
    assert "laptop" not in after
    assert after["phone"]["rx"] == 1_400


def test_settings_and_quota(client, settings, host):
    seed(settings, host)
    client.post("/api/auth/login", json={"password": "s3cret"})
    r = client.put("/api/settings", json={"billing_day": 5, "quota_gb": 140})
    assert r.status_code == 200 and r.json()["billing_day"] == 5
    quota = client.get("/api/overview").json()["quota"]
    assert quota["bytes"] == 140 * 1000**3 and quota["used"] == 1_465  # no provider yet: decimal GB


def test_lockout_after_repeated_failures(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"password": "nope"})
    assert client.post("/api/auth/login", json={"password": "s3cret"}).status_code == 429


def test_spa_fallback_and_api_404(client):
    assert client.get("/devices/12").text.startswith("<!doctype html>")
    assert client.get("/api/nope").status_code == 404


def test_health_checks_tattle_against_the_provider_on_the_last_full_day(make_settings):
    import datetime as dt
    import time

    from tattle import db

    settings = make_settings(mode="gateway")
    client = TestClient(create_app(settings, start_collector=False))
    conn = db.connect(settings.db_path)
    coverage = next(c for c in client.get("/api/health").json()["checks"] if c["id"] == "coverage")
    assert coverage["ok"] is True and "Connect your provider" in coverage["detail"]

    yesterday = dt.datetime.now(settings.timezone).date() - dt.timedelta(days=1)
    start = int(dt.datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=settings.timezone).timestamp())
    conn.execute("INSERT INTO devices(id, key, first_seen, last_seen) VALUES(900, 'mac:y', 0, 0)")
    conn.execute("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(900, ?, ?, 0)", (start - 3600, 1))
    conn.execute("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(900, ?, ?, 0)", (start + 3600, 69 * 1024**3))
    conn.execute("INSERT INTO isp_daily(number, day, bytes) VALUES('0221234567', ?, ?)", (yesterday.isoformat(), 100 * 1024**3))
    coverage = next(c for c in client.get("/api/health").json()["checks"] if c["id"] == "coverage")
    assert coverage["ok"] is False and "Tattle counted 69% of what your provider charged" in coverage["detail"]

    conn.execute("UPDATE isp_daily SET bytes = ?", (72 * 1024**3,))
    coverage = next(c for c in client.get("/api/health").json()["checks"] if c["id"] == "coverage")
    assert coverage["ok"] is True and int(time.time()) > start


def test_split_undoes_a_merge_and_suggestions_flow(client, settings, host):
    seed(settings, host)
    devices = {d["name"]: d for d in client.get("/api/devices").json()["devices"]}
    phone, laptop = devices["phone"]["id"], devices["laptop"]["id"]
    client.post("/api/auth/login", json={"password": "s3cret"})
    client.post(f"/api/devices/{laptop}/merge", json={"into": phone})
    merged = client.get(f"/api/devices/{phone}").json()["device"]
    assert [i["id"] for i in merged["identities"]] == [phone, laptop]
    assert client.get(f"/api/devices/{laptop}").status_code == 409

    assert client.post(f"/api/devices/{laptop}/split").status_code == 200
    names = {d["name"] for d in client.get("/api/devices").json()["devices"]}
    assert {"phone", "laptop"} <= names

    from tattle import db

    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT status FROM merge_suggestions").fetchone()["status"] == "dismissed"
    conn.execute("UPDATE merge_suggestions SET status = 'open'")
    (suggestion,) = client.get("/api/suggestions").json()["suggestions"]
    assert client.post(f"/api/suggestions/{suggestion['id']}/accept").status_code == 200
    assert client.get("/api/suggestions").json()["suggestions"] == []


def test_generic_dhcp_name_loses_to_announced_name(client, settings, host):
    from tattle import db
    from tattle.collector import Collector, LiveState

    host.write_leases(("0a:00:5e:00:53:6c", "192.168.1.8", "iPhone", None))
    db.init(settings.db_path)
    Collector(settings, LiveState(), clock=host_clock(settings)).tick()
    conn = db.connect(settings.db_path)
    device = conn.execute("SELECT id FROM devices WHERE key = 'mac:0a:00:5e:00:53:6c'").fetchone()["id"]
    conn.execute(
        "INSERT INTO device_names VALUES(?, 'mdns', 'Mayas-iPhone-SE', 'mayas-iphone-se', 1, 1)", (device,)
    )
    names = [d["name"] for d in client.get("/api/devices").json()["devices"]]
    assert "Mayas-iPhone-SE" in names


def test_the_newest_typed_name_wins_after_a_merge(client, settings, host):
    seed(settings, host)
    devices = {d["name"]: d for d in client.get("/api/devices").json()["devices"]}
    phone, laptop = devices["phone"]["id"], devices["laptop"]["id"]
    client.post("/api/auth/login", json={"password": "s3cret"})
    client.patch(f"/api/devices/{phone}", json={"name": "Old name"})
    import time as _time

    _time.sleep(1.1)
    client.patch(f"/api/devices/{laptop}", json={"name": "Maya's Phone"})
    client.post(f"/api/devices/{laptop}/merge", json={"into": phone})
    assert client.get(f"/api/devices/{phone}").json()["device"]["name"] == "Maya's Phone"
    # Renaming the merged device leaves one name.
    client.patch(f"/api/devices/{phone}", json={"name": "Maya's iPhone"})
    assert client.get(f"/api/devices/{phone}").json()["device"]["name"] == "Maya's iPhone"
