from __future__ import annotations

import json
from pathlib import Path

from tattle.gateway import status
from tattle.gateway.preflight import Check


def test_shape_for_the_dashboard(tmp_path: Path):
    report = status.GatewayStatus(str(tmp_path / "gateway.json"), "eth0", "192.0.2.10", "192.0.2.1")
    report.set(preflight=[Check("ip_forward", True, "IP forwarding is on.")], leases=3, state="running")
    report.probed(now=1_791_700_000)
    report.write()
    data = json.loads((tmp_path / "gateway.json").read_text())
    assert data["version"] == 1 and isinstance(data["updated"], int)
    assert (data["lan_if"], data["server_ip"], data["router_ip"]) == ("eth0", "192.0.2.10", "192.0.2.1")
    assert data["preflight"] == [{"id": "ip_forward", "ok": True, "detail": "IP forwarding is on."}]
    assert set(data["forwarding"]) == {"ok", "detail"}
    assert data["dhcp"]["leases"] == 3 and data["dhcp"]["foreign_servers"] == []
    assert data["dhcp"]["probed_at"] == 1_791_700_000
    assert set(data["ipv6_adverts"]) >= {"seen", "routers", "checked_at"}
    assert oct((tmp_path / "gateway.json").stat().st_mode & 0o777) == "0o644"


def test_foreign_servers_are_reported_then_forgotten():
    report = status.GatewayStatus("/nonexistent")
    assert report.saw_foreign_server("192.0.2.1", "00:00:5e:00:53:66", now=1000)
    assert not report.saw_foreign_server("192.0.2.1", "00:00:5e:00:53:66", now=1500)  # already known
    data = report.as_dict(now=1600)
    assert data["dhcp"]["foreign_servers"] == [{"ip": "192.0.2.1", "mac": "00:00:5e:00:53:66", "seen": 1500}]
    assert "192.0.2.1" in data["dhcp"]["detail"] and "Off (not Auto)" in data["dhcp"]["detail"]
    assert report.as_dict(now=1500 + status.FOREIGN_TTL)["dhcp"]["foreign_servers"] == []


def test_read_tolerates_missing_and_broken_files(tmp_path: Path):
    assert status.read(str(tmp_path / "missing.json")) is None
    (tmp_path / "broken.json").write_text("{not json")
    assert status.read(str(tmp_path / "broken.json")) is None
    (tmp_path / "list.json").write_text("[]")
    assert status.read(str(tmp_path / "list.json")) is None
