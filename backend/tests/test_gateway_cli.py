from __future__ import annotations

import json
import time
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from tattle.gateway import __main__ as cli
from tattle.gateway import dockerfw, nft, preflight, service
from tattle.gateway.config import GatewayConfig
from tattle.gateway.paths import Paths

CFG = GatewayConfig(
    lan_if="eth0",
    server_ip=IPv4Address("192.0.2.10"),
    lan=IPv4Network("192.0.2.0/24"),
    router_ip=IPv4Address("192.0.2.1"),
    dhcp_start=IPv4Address("192.0.2.100"),
    dhcp_end=IPv4Address("192.0.2.200"),
)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(str(tmp_path / "run"), str(tmp_path / "state"))
    p.create()
    return p


@pytest.fixture
def removed(monkeypatch):
    calls = []
    monkeypatch.setattr(nft, "remove_table", lambda: calls.append("table") or True)
    monkeypatch.setattr(dockerfw, "apply", lambda lan_if: calls.append(("docker", lan_if)) or dockerfw.ForwardState(True))
    return calls


def write_status(paths: Paths, state: str, age: float = 0) -> None:
    Path(paths.status).write_text(json.dumps({"version": 1, "state": state, "updated": time.time() - age, "router_ip": "192.0.2.1"}))


def test_off_refuses_while_the_service_runs(paths, removed, capsys):
    write_status(paths, "running")
    assert cli.cmd_off(paths, force=False) == 1
    assert "docker compose --profile gateway rm -sf gateway" in capsys.readouterr().err
    assert removed == []


def test_off_removes_rules_and_explains_the_way_back(paths, removed, capsys):
    write_status(paths, "running", age=600)  # a crashed service's last report
    assert cli.cmd_off(paths, force=False) == 0
    assert removed == ["table", ("docker", None)]
    out = capsys.readouterr().out
    assert "http://192.0.2.1" in out and "DHCP server back on" in out and "Wi-Fi off and on" in out
    assert json.loads(Path(paths.status).read_text())["state"] == "off"


def test_run_refuses_with_reasons_on_record(paths, monkeypatch, capsys):
    bad = preflight.Check("ip_forward", False, "IP forwarding is off.")
    monkeypatch.setattr(preflight, "run_checks", lambda cfg: [preflight.Check("kernel", True, "ok"), bad])
    monkeypatch.setattr(nft.Table, "ensure", lambda *a, **k: pytest.fail("must not touch the firewall"))
    monkeypatch.setattr(service.signal, "signal", lambda *a: None)  # keep pytest's own Ctrl-C handling
    assert service.Gateway(CFG, paths).run() == 2
    data = json.loads(Path(paths.status).read_text())
    assert data["state"] == "refused"
    assert [c["ok"] for c in data["preflight"]] == [True, False]
    assert "IP forwarding is off." in capsys.readouterr().err


def test_bad_config_is_recorded(paths, monkeypatch):
    monkeypatch.setenv("TATTLE_LAN_IF", "eth0\nbad")
    assert cli.cmd_run(paths) == 2
    data = json.loads(Path(paths.status).read_text())
    assert data["state"] == "refused" and data["preflight"][0]["id"] == "config"


def test_restart_keeps_the_last_verdicts(paths):
    report = {
        "version": 1,
        "state": "stopped",
        "updated": time.time() - 60,
        "forwarding": {"ok": False, "detail": "Another firewall is dropping it."},
        "dhcp": {"foreign_servers": [{"ip": "192.0.2.1", "mac": "00:00:5e:00:53:66", "seen": int(time.time()) - 60}]},
        "ipv6_adverts": {"seen": True, "routers": ["fe80::1"], "checked_at": int(time.time()) - 60, "detail": "x"},
    }
    Path(paths.status).write_text(json.dumps(report))
    gateway = service.Gateway(CFG, paths)
    gateway.resume_status()
    data = gateway.status.as_dict()
    assert data["forwarding"] == {"ok": False, "detail": "Another firewall is dropping it."}
    assert gateway.forwarding.ok is False  # the self-test carries on from the old verdict
    assert [s["ip"] for s in data["dhcp"]["foreign_servers"]] == ["192.0.2.1"]
    assert data["ipv6_adverts"]["seen"] is True

    report["updated"] = time.time() - 3600  # an old report is not trusted
    Path(paths.status).write_text(json.dumps(report))
    fresh = service.Gateway(CFG, paths)
    fresh.resume_status()
    assert fresh.status.as_dict()["forwarding"]["ok"] is None
