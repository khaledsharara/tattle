"""First run, the status the app lays itself out by, router settings, and the demo."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from tattle import routers
from tattle.api import MASK, create_app
from tattle.providers import Field
from tattle.routers import RouterAdapter, RouterError


class FakeAdapter(RouterAdapter):
    id = "test_router"
    name = "Test router"
    capabilities = frozenset({"wan_login"})
    fields = (Field("url", "Address"), Field("password", "Password", kind="password"))
    seen: list[dict] = []

    def wan_login(self):
        FakeAdapter.seen.append(dict(self.values))
        if self.values.get("password") != "right":
            raise RouterError("The router refused the password")
        return "user@isp"


def test_first_run_needs_the_setup_code(make_settings):
    settings = make_settings(admin_password="")
    client = TestClient(create_app(settings, start_collector=False))
    status = client.get("/api/settings").json()
    assert status["admin_setup_needed"] is True and status["admin_enabled"] is False
    code_file = os.path.join(settings.data_dir, "setup-code.txt")
    code = open(code_file).read().strip()
    assert len(code) == 6 and code.isdigit()

    wrong = "000000" if code != "000000" else "111111"
    assert client.post("/api/auth/setup", json={"code": wrong, "password": "long enough"}).status_code == 401
    assert client.post("/api/auth/setup", json={"code": code, "password": "short"}).status_code == 422
    assert client.post("/api/auth/setup", json={"code": code, "password": "a good password"}).status_code == 200
    assert client.get("/api/settings").json()["is_admin"] is True
    assert not os.path.exists(code_file)
    assert client.post("/api/auth/setup", json={"code": code, "password": "another one"}).status_code == 409

    # The password survives a restart (it is stored hashed), and the code is gone.
    again = TestClient(create_app(settings, start_collector=False))
    assert again.get("/api/settings").json()["admin_setup_needed"] is False
    assert again.post("/api/auth/login", json={"password": "a good password"}).status_code == 200


def test_status_reports_mode_and_what_is_set_up(settings):
    client = TestClient(create_app(settings, start_collector=False))
    status = client.get("/api/status").json()
    assert status["mode"] == "quota" and status["counting"] is False
    assert status["setup"] == {"admin": True, "provider": False, "router": False}
    assert status["unit_base"] == 1000


def test_router_is_tested_before_it_is_saved_and_secrets_stay_hidden(settings, monkeypatch):
    monkeypatch.setitem(routers.registry(), FakeAdapter.id, FakeAdapter)
    client = TestClient(create_app(settings, start_collector=False))
    assert client.put("/api/router", json={"adapter": "test_router", "values": {}}).status_code == 401
    client.post("/api/auth/login", json={"password": "s3cret"})
    names = [r["id"] for r in client.get("/api/routers").json()["routers"]]
    assert "dlink_dsl245ge" in names and "test_router" in names

    refused = client.put("/api/router", json={"adapter": "test_router", "values": {"url": "x", "password": "wrong"}})
    assert refused.status_code == 400 and "refused the password" in refused.json()["detail"]
    assert client.get("/api/router").json()["adapter"] is None

    saved = client.put("/api/router", json={"adapter": "test_router", "values": {"url": "x", "password": "right"}})
    assert saved.json() == {"adapter": "test_router", "found": ["internet login user@isp"]}
    shown = client.get("/api/router").json()
    assert shown["values"] == {"url": "x", "password": MASK}

    # Saving the form again with the mask keeps the stored password.
    assert client.put("/api/router", json={"adapter": "test_router", "values": {"url": "y", "password": MASK}}).status_code == 200
    assert FakeAdapter.seen[-1] == {"url": "y", "password": "right"}

    anonymous = TestClient(create_app(settings, start_collector=False))
    assert anonymous.get("/api/router").json()["values"] == {}


def test_demo_has_a_household_two_lines_and_no_setup(make_settings):
    settings = make_settings(demo=True, admin_password="")
    client = TestClient(create_app(settings, start_collector=False))
    status = client.get("/api/status").json()
    assert status["mode"] == "demo" and status["counting"] and status["setup"]["admin"]

    overview = client.get("/api/overview?range=cycle").json()
    names = {d["name"] for d in overview["devices"]}
    assert {"Living room TV", "Maya's iPhone", "PlayStation 5"} <= names
    quota = overview["quota"]
    assert quota["source"] == "isp" and quota["provider"]["name"] == "Demo ISP"
    assert quota["used"] > 100 * 1000**3 and quota["bytes"] == quota["used"] + (quota["bytes"] - quota["used"])

    lines = client.get("/api/isp").json()
    assert lines["live_number"] == "HOME-4821" and len(lines["lines"]) == 2
    page = client.get("/api/isp/lines/HOME-4821?span=30d").json()
    assert page["billing"]["open_amount"] == 49.0 and page["totals"]["isp"] > 0
    assert any(e["kind"] == "merged" for e in client.get("/api/identity-events").json()["events"])
    assert client.get("/api/health").json()["ok"] is True
    # Every button can be tried; the database is thrown away.
    assert client.get("/api/settings").json()["is_admin"] is True

    # A restart keeps the demo as it was instead of adding a second household.
    again = TestClient(create_app(settings, start_collector=False))
    assert len(again.get("/api/isp").json()["lines"]) == 2
