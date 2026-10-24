"""OpenWrt nlbwmon adapter against a fake router.

The nlbw JSON follows nlbwmon's client.c (handle_json) and what `nlbw -c json
-g mac,ip` printed on OpenWrt 25.12.6 (nlbwmon 2025-06-02) in a test
container. The /ubus answers follow rpcd (session login, file exec) as seen on
the same build. MACs are from the RFC 7042 documentation range; addresses,
names and passwords are made up.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import urllib.parse

import pytest

from tattle.providers.http import Response
from tattle.routers import openwrt_nlbwmon, registry
from tattle.routers.base import RouterError
from tattle.routers.openwrt_nlbwmon import NLBW, NLBW_ACTION, OpenwrtNlbwmon, key_file_text, parse_nlbw

NLBW_JSON = json.dumps(
    {
        "columns": ["mac", "ip", "conns", "rx_bytes", "rx_pkts", "tx_bytes", "tx_pkts"],
        "data": [
            ["00:00:5e:00:53:10", "192.168.1.20", 12, 5_113_036, 2382, 81_691, 2000],
            ["00:00:5e:00:53:10", "2001:db8::20", 4, 900_000, 700, 30_000, 300],
            ["00:00:5e:00:53:10", "2001:db8::21", 1, 10, 1, 10, 1],
            ["00:00:5E:00:53:11", "192.168.1.61", 3, 2_000, 10, 2_132_421, 1500],
            ["00:00:00:00:00:00", "192.168.1.99", 1, 777, 2, 777, 2],  # MAC not found by nlbwmon
        ],
    }
)
PASSWORD = "router-pass-1"
SID_PREFIX = "c552bc8537b5942c4c466ef9310300"
# Assembled at run time so secret scanners don't mistake these fakes for leaked keys.
PRIVATE_KEY = "PRIVATE" + " KEY"


def armour(kind: str, *lines: str) -> str:
    return "\n".join([f"-----BEGIN {kind} {PRIVATE_KEY}-----", *lines, f"-----END {kind} {PRIVATE_KEY}-----", ""])


def fake_key(cipher: bytes = b"none") -> str:
    """Shaped like an OpenSSH private key (PROTOCOL.key header); not a usable key."""
    raw = b"openssh-key-v1\0" + len(cipher).to_bytes(4, "big") + cipher + bytes(range(256)) * 2
    body = base64.b64encode(raw).decode()
    return armour("OPENSSH", *(body[i : i + 70] for i in range(0, len(body), 70)))


# --- parsing -------------------------------------------------------------------------------
def test_parse_adds_up_addresses_per_mac_and_maps_direction():
    counters = {c.mac: c for c in parse_nlbw(NLBW_JSON)}
    assert set(counters) == {"00:00:5e:00:53:10", "00:00:5e:00:53:11"}  # lower case; the 00:00:... row is dropped
    phone = counters["00:00:5e:00:53:10"]
    assert phone.rx == 5_113_036 + 900_000 + 10  # rx_bytes is what the device downloaded
    assert phone.tx == 81_691 + 30_000 + 10
    assert phone.ip == "192.168.1.20"  # the IPv4 address, not a changing IPv6 one
    assert counters["00:00:5e:00:53:11"].tx == 2_132_421


def test_parse_reads_columns_by_name():
    text = json.dumps({"columns": ["conns", "tx_bytes", "rx_bytes", "mac"], "data": [[1, 20, 10, "00:00:5e:00:53:12"]]})
    (counter,) = parse_nlbw(text)
    assert (counter.rx, counter.tx, counter.ip) == (10, 20, None)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "<html>",
        '{"columns": ["ip"], "data": []}',
        '{"data": []}',
        '{"columns": ["mac", "rx_bytes", "tx_bytes"], "data": [["00:00:5e:00:53:10", 1]]}',
        '{"columns": ["mac", "rx_bytes", "tx_bytes"], "data": [["00:00:5e:00:53:10", "many", 1]]}',
    ],
)
def test_parse_rejects_other_answers(text):
    with pytest.raises(RouterError):
        parse_nlbw(text)


def test_registered_as_beta():
    assert registry()["openwrt_nlbwmon"] is OpenwrtNlbwmon
    assert OpenwrtNlbwmon.describe()["status"] == "beta"
    assert OpenwrtNlbwmon.device_modulus is None  # nlbwmon keeps 64-bit totals


# --- the web API (uhttpd /ubus + rpcd) --------------------------------------------------------
class FakeUbus:
    """uhttpd-mod-ubus and rpcd: session login, then file exec checked against an ACL."""

    def __init__(self, *, allowed=((NLBW, ("-c", "json", "-g", "mac,ip")),), exec_ok=True, redirect_to=None, missing=False):
        self.allowed = {(cmd, tuple(args)) for cmd, args in allowed}
        self.exec_ok = exec_ok  # False: the account may sign in but not use file exec at all
        self.redirect_to = redirect_to
        self.missing = missing
        self.sessions: set[str] = set()
        self.logins = 0
        self.execs: list[tuple[str, tuple]] = []
        self.nlbw_result = {"code": 0, "stdout": NLBW_JSON}
        self.urls: list[str] = []

    def __call__(self, request) -> Response:
        self.urls.append(request.url)
        if self.missing:
            return Response(404, {}, "Not Found", request.url)
        if self.redirect_to and request.url.startswith("http://"):
            return Response(307, {"location": self.redirect_to + "/ubus"}, "", request.url)
        assert request.method == "POST" and urllib.parse.urlsplit(request.url).path == "/ubus"
        call = json.loads(request.body)
        sid, obj, method, args = call["params"]
        if (obj, method) == ("session", "login"):
            if args != {"username": "root", "password": PASSWORD}:
                return self._result([6])
            self.logins += 1
            sid = f"{SID_PREFIX}{self.logins:02d}"
            self.sessions.add(sid)
            return self._result([0, {"ubus_rpc_session": sid, "timeout": 300, "expires": 300}])
        if sid not in self.sessions or not self.exec_ok:
            return self._json({"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "Access denied"}})
        assert (obj, method) == ("file", "exec")
        entry = (args["command"], tuple(args["params"]))
        self.execs.append(entry)
        if entry not in self.allowed:
            return self._result([6])
        return self._result([0, self.nlbw_result])

    def _result(self, result):
        return self._json({"jsonrpc": "2.0", "id": 1, "result": result})

    @staticmethod
    def _json(body):
        return Response(200, {"content-type": "application/json"}, json.dumps(body), "http://192.0.2.1/ubus")


def _web(fake, **values):
    return OpenwrtNlbwmon({"host": "192.0.2.1", "password": PASSWORD, **values}, transport=fake)


def test_web_reads_counters_with_tattles_access_rule():
    fake = FakeUbus()
    router = _web(fake)
    assert router.test() == ["2 devices with traffic counters", "read through the router's web API"]
    assert fake.urls[0] == "http://192.0.2.1/ubus"
    assert fake.execs == [(NLBW, ("-c", "json", "-g", "mac,ip"))]


def test_web_uses_luci_helper_for_admins_and_remembers_it():
    fake = FakeUbus(allowed=[(NLBW_ACTION, ("download", "-g", "mac,ip"))])  # stock LuCI ACLs, root
    router = _web(fake)
    assert len(router.device_counters()) == 2
    assert [cmd for cmd, _ in fake.execs] == [NLBW, NLBW_ACTION]  # nlbw itself refused (6), then the helper
    router.device_counters()
    assert [cmd for cmd, _ in fake.execs][2:] == [NLBW_ACTION]
    assert fake.logins == 1  # the session is kept between readings


def test_web_signs_in_again_once_when_the_session_ran_out():
    fake = FakeUbus()
    router = _web(fake)
    router.device_counters()
    fake.sessions.clear()  # rpcd restarted, or five idle minutes passed
    assert len(router.device_counters()) == 2
    assert fake.logins == 2


def test_web_account_that_may_not_exec():
    fake = FakeUbus(exec_ok=False)
    with pytest.raises(RouterError, match="may not run programs"):
        _web(fake).device_counters()
    assert fake.logins == 2  # one retry, no loop


def test_web_account_without_any_nlbw_rule():
    fake = FakeUbus(allowed=[])
    with pytest.raises(RouterError, match="luci-app-nlbwmon"):
        _web(fake).device_counters()


def test_web_wrong_password():
    with pytest.raises(RouterError, match="refused the user name or password"):
        _web(FakeUbus(), password="wrong").device_counters()


def test_web_follows_the_redirect_to_https():
    fake = FakeUbus(redirect_to="https://192.0.2.1")
    router = _web(fake)
    assert len(router.device_counters()) == 2
    assert router.base_url == "https://192.0.2.1"
    assert fake.urls[-1] == "https://192.0.2.1/ubus"


def test_web_without_ubus():
    with pytest.raises(RouterError, match="no web API at /ubus"):
        _web(FakeUbus(missing=True)).device_counters()


def test_web_nlbwmon_not_running():
    fake = FakeUbus()
    fake.nlbw_result = {"code": 247, "stdout": "", "stderr": "Error while processing command: Bad file descriptor\n"}
    with pytest.raises(RouterError, match="nlbwmon is not running"):
        _web(fake).device_counters()


def test_needs_a_password_or_a_key():
    with pytest.raises(RouterError, match="password, or paste an SSH key"):
        OpenwrtNlbwmon({"host": "192.0.2.1"}, transport=FakeUbus()).device_counters()


def test_unreachable_router():
    router = OpenwrtNlbwmon({"host": "127.0.0.1:1", "password": PASSWORD})  # nothing listens on port 1
    with pytest.raises(RouterError, match="Could not reach the router"):
        router.device_counters()


# --- SSH -----------------------------------------------------------------------------------
class FakeSsh:
    """Stands in for subprocess.run: checks the key file while ssh would run."""

    def __init__(self, returncode=0, stdout=NLBW_JSON, stderr="", raises=None):
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises
        self.commands: list[list[str]] = []
        self.key_path: str | None = None
        self.key_mode: int | None = None
        self.key_text: str | None = None

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.key_path = command[command.index("-i") + 1]
        self.key_mode = stat.S_IMODE(os.stat(self.key_path).st_mode)
        with open(self.key_path) as handle:
            self.key_text = handle.read()
        assert kwargs["timeout"] > 0 and kwargs["capture_output"]
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def ssh(monkeypatch, tmp_path):
    monkeypatch.setenv("TATTLE_DB", str(tmp_path / "tattle.db"))

    def install(**kwargs) -> FakeSsh:
        fake = FakeSsh(**kwargs)
        monkeypatch.setattr(openwrt_nlbwmon.subprocess, "run", fake)
        return fake

    return install


def test_ssh_runs_nlbw_with_a_private_key_file(ssh, tmp_path):
    fake = ssh()
    key = fake_key()
    flattened = " ".join(key.split("\n"))  # what a password box does to a pasted key (Firefox)
    router = OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": flattened, "ssh_port": "2222"})
    assert router.test() == ["2 devices with traffic counters", "read over SSH"]
    command = fake.commands[0]
    assert command[0] == "ssh" and command[command.index("-p") + 1] == "2222"
    for option in ("BatchMode=yes", "StrictHostKeyChecking=accept-new", f"UserKnownHostsFile={tmp_path / 'ssh_known_hosts'}"):
        assert option in command
    assert command[-3:] == ["--", "root@192.0.2.1", "/usr/sbin/nlbw -c json -g mac,ip"]
    assert fake.key_mode == 0o600
    assert fake.key_text == key  # line breaks back where ssh-keygen put them
    assert not os.path.exists(fake.key_path)  # removed afterwards


def test_ssh_key_without_line_breaks_at_all(ssh):
    fake = ssh()
    OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": fake_key().replace("\n", "")}).device_counters()
    assert fake.key_text == fake_key()


@pytest.mark.parametrize(
    ("returncode", "stderr", "message"),
    [
        (255, "root@192.0.2.1: Permission denied (publickey,password).\n", "refused the SSH key"),
        (255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n", "SSH identity changed"),
        (255, "ssh: connect to host 192.0.2.1 port 22: Connection refused\n", "refused SSH connections on port 22"),
        (255, "ssh: connect to host 192.0.2.1 port 22: Operation timed out\n", "Could not reach the router over SSH: ssh: connect"),
        (255, "No user exists for uid 1000\n", "no account"),
        (127, "ash: /usr/sbin/nlbw: not found\n", "install the nlbwmon package"),
        (247, "Error while processing command: Bad file descriptor\n", "nlbwmon is not running"),
    ],
)
def test_ssh_failures_read_well(ssh, returncode, stderr, message):
    ssh(returncode=returncode, stdout="", stderr=stderr)
    key = fake_key()
    with pytest.raises(RouterError, match=message) as caught:
        OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": key}).device_counters()
    assert PRIVATE_KEY not in str(caught.value) and key.splitlines()[1] not in str(caught.value)


def test_ssh_program_missing_or_slow(ssh):
    ssh(raises=FileNotFoundError("ssh"))
    with pytest.raises(RouterError, match="openssh-client"):
        OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": fake_key()}).device_counters()
    fake = ssh(raises=subprocess.TimeoutExpired("ssh", 30))
    with pytest.raises(RouterError, match="did not answer over SSH"):
        OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": fake_key()}).device_counters()
    assert not os.path.exists(fake.key_path)


@pytest.mark.parametrize(
    ("key", "message"),
    [
        (fake_key(b"aes256-ctr"), "passphrase"),
        (armour("RSA", "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,00", "AAAA"), "passphrase"),
        ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample tattle", "not a private key"),
    ],
)
def test_ssh_unusable_keys(ssh, key, message):
    fake = ssh()
    with pytest.raises(RouterError, match=message):
        OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": key}).device_counters()
    assert fake.commands == []


@pytest.mark.parametrize(("values", "message"), [({"host": "-oProxyCommand=x"}, "not a host name"), ({"user": "root -o x"}, "user name"), ({"ssh_port": "22; reboot"}, "SSH port")])
def test_ssh_refuses_values_that_could_become_options(ssh, values, message):
    fake = ssh()
    with pytest.raises(RouterError, match=message):
        OpenwrtNlbwmon({"host": "192.0.2.1", "ssh_key": fake_key(), **values}).device_counters()
    assert fake.commands == []


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
@pytest.mark.parametrize("keygen_args", [["-t", "ed25519"], ["-t", "rsa", "-b", "2048", "-m", "PEM"]])
def test_rebuilt_real_keys_still_load(tmp_path, keygen_args):
    """A key made right now (never stored), squashed onto one line, rebuilt, read back by ssh-keygen."""
    path = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-N", "", "-C", "tattle-test", "-f", str(path), *keygen_args], check=True)
    squashed = path.read_text().replace("\n", " ")
    rebuilt = tmp_path / "rebuilt"
    rebuilt.write_text(key_file_text(squashed))
    rebuilt.chmod(0o600)
    public = subprocess.run(["ssh-keygen", "-y", "-f", str(rebuilt)], check=True, capture_output=True, text=True).stdout
    assert public.split()[:2] == (tmp_path / "key.pub").read_text().split()[:2]
