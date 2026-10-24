"""OpenWrt with nlbwmon, the traffic accounting behind LuCI's "Bandwidth Monitor".

nlbwmon follows the router's connection tracking and adds each connection's
bytes to the LAN device it came from (found by MAC), for the current
accounting period. It counts only connections between the networks in its
`local_network` list and everything else, so traffic that stays in the house
(device to device, device to the router itself) is left out. For the local
device, nlbwmon books the reply direction of a connection it opened as "in":
`rx_bytes` is what the device downloaded (nfnetlink.c, parse_event).

Tattle runs `nlbw -c json -g mac,ip` one of two ways:

* through the web API LuCI itself uses (uhttpd's /ubus JSON-RPC, an rpcd
  session, `file exec`), with the router's sign-in;
* over SSH with a key, using the system ssh client.

Totals restart at the start of each accounting period and when the router
reboots (nlbwmon keeps them in RAM by default); Tattle treats a drop as a
restart. See docs/routers/openwrt_nlbwmon.md.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.parse

from ..providers.base import Field, ProviderError
from ..providers.http import Http, Transport
from .base import DeviceCounter, RouterAdapter, RouterError

NLBW = "/usr/sbin/nlbw"
NLBW_ARGS = ["-c", "json", "-g", "mac,ip"]
# luci-app-nlbwmon's helper; its rpcd ACL lets LuCI's admins run it with these arguments.
NLBW_ACTION = "/usr/libexec/nlbwmon-action"
NLBW_ACTION_ARGS = ["download", "-g", "mac,ip"]
NO_SESSION = "0" * 32
SSH_TIMEOUT = 30

# ubus status codes (libubus); file exec answers [code] when it refuses.
UBUS_PERMISSION_DENIED = 6
UBUS_NOT_FOUND = 4
JSONRPC_ACCESS_DENIED = -32002  # session unknown or expired, or the ubus call itself not allowed

_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def known_hosts_path() -> str:
    """The SSH host keys Tattle has seen, kept next to its database so they
    survive container rebuilds (TATTLE_DB, as in config.py)."""
    data_dir = os.path.dirname(os.environ.get("TATTLE_DB", "/data/tattle.db")) or "."
    return os.path.join(data_dir, "ssh_known_hosts")


def key_file_text(pasted: str) -> str:
    """The private key as ssh wants it on disk.

    A password box drops the line breaks of pasted text (browsers strip them or
    turn them into spaces), so the key is rebuilt from its BEGIN/END lines.
    """
    match = _PEM.search(pasted or "")
    if not match:
        raise RouterError("The SSH key is not a private key: paste all of it, from -----BEGIN to the -----END line")
    kind, body = match.group(1), "".join(match.group(2).split())
    passphrase = RouterError("The SSH key has a passphrase. Tattle needs a key made without one (ssh-keygen -N '')")
    if "ENCRYPTED" in kind or "Proc-Type" in body:
        raise passphrase
    width = 64
    if kind == "OPENSSH PRIVATE KEY":
        width = 70  # what ssh-keygen writes
        try:
            raw = base64.b64decode(body, validate=True)
        except ValueError:
            raise RouterError("The SSH key is damaged: paste it again, all of it") from None
        magic = b"openssh-key-v1\0"  # then the cipher name as a length-prefixed string (OpenSSH PROTOCOL.key)
        if raw.startswith(magic):
            size = int.from_bytes(raw[len(magic) : len(magic) + 4], "big")
            if raw[len(magic) + 4 : len(magic) + 4 + size] != b"none":
                raise passphrase
    lines = [body[i : i + width] for i in range(0, len(body), width)]
    return "\n".join([f"-----BEGIN {kind}-----", *lines, f"-----END {kind}-----", ""])


def parse_nlbw(text: str) -> list[DeviceCounter]:
    """`nlbw -c json -g mac,ip` prints {"columns": [...], "data": [[...], ...]}
    with one row per MAC and address (client.c, handle_json). A device has a
    row for each of its IPv4 and IPv6 addresses; Tattle adds them up per MAC."""
    try:
        data = json.loads(text)
        columns = list(data["columns"])
        rows = list(data["data"])
    except (ValueError, KeyError, TypeError):
        raise RouterError("nlbwmon's answer was not the JSON Tattle expected") from None
    if not {"mac", "rx_bytes", "tx_bytes"} <= set(columns):
        raise RouterError("nlbwmon's answer has no per-device byte columns")
    totals: dict[str, list] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise RouterError("nlbwmon's answer has a row Tattle could not read")
        record = dict(zip(columns, row, strict=True))
        mac = str(record.get("mac") or "").lower()
        if not mac or mac == "00:00:00:00:00:00":  # nlbwmon could not find the device's MAC
            continue
        try:
            rx, tx = int(record["rx_bytes"]), int(record["tx_bytes"])
        except (TypeError, ValueError):
            raise RouterError("nlbwmon's answer has a byte count Tattle could not read") from None
        entry = totals.setdefault(mac, [0, 0, None, -1])
        entry[0] += rx
        entry[1] += tx
        ip = record.get("ip")
        # IPv4 addresses are the stable ones; IPv6 privacy addresses change daily.
        if isinstance(ip, str) and "." in ip and rx + tx > entry[3]:
            entry[2], entry[3] = ip, rx + tx
    return [DeviceCounter(mac, rx, tx, ip) for mac, (rx, tx, ip, _) in sorted(totals.items())]


class OpenwrtNlbwmon(RouterAdapter):
    id = "openwrt_nlbwmon"
    name = "OpenWrt (nlbwmon)"
    status = "beta"
    capabilities = frozenset({"device_counters"})
    fields = (
        Field("host", "Router address", placeholder="192.168.1.1", help="Its IP address or name. For the web sign-in you may write https:// in front."),
        Field("user", "User name", placeholder="root", optional=True),
        Field("password", "Router password", kind="password", optional=True, help="The LuCI sign-in. Needs luci-app-nlbwmon on the router. Leave empty to use an SSH key instead."),
        Field("ssh_key", "SSH private key", kind="password", optional=True, help="Instead of the password: paste the whole private key, BEGIN and END lines included."),
        Field("ssh_port", "SSH port", placeholder="22", optional=True),
    )
    notes = "Reads nlbwmon's per-device totals for its current period. Counts internet traffic only; not traffic that stays in the house."
    docs = "docs/routers/openwrt_nlbwmon.md"

    def __init__(self, values: dict[str, str], transport: Transport | None = None):
        super().__init__(values)
        self.transport = transport
        address = (values.get("host") or "192.168.1.1").strip()
        self.base_url = address if "://" in address else f"http://{address}"
        self.host = urllib.parse.urlsplit(self.base_url).hostname or ""
        self.user = (values.get("user") or "root").strip()
        self.password = values.get("password") or ""
        self.ssh_key = values.get("ssh_key") or ""
        self.ssh_port = (values.get("ssh_port") or "22").strip()
        self.http = self._client(self.base_url)
        self.session: str | None = None
        self.command: tuple[str, list[str]] | None = None  # the exec the router allowed last time

    @property
    def way(self) -> str:
        return "ssh" if self.ssh_key.strip() else "web"

    def device_counters(self) -> list[DeviceCounter]:
        if self.way == "ssh":
            return parse_nlbw(self._ssh())
        if not self.password:
            raise RouterError("Fill in the router password, or paste an SSH key")
        return parse_nlbw(self._web())

    def test(self) -> list[str]:
        how = "over SSH" if self.way == "ssh" else "through the router's web API"
        return [*super().test(), f"read {how}"]

    # --- the web API (uhttpd /ubus + rpcd) -------------------------------------------
    def _client(self, base_url: str) -> Http:
        # OpenWrt's HTTPS certificate is one the router made for itself; it cannot be checked.
        return Http(base_url, timeout=15.0, transport=self.transport, verify_tls=False, who="the router", error=RouterError)

    def _call(self, sid: str, obj: str, method: str, args: dict) -> list:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "call", "params": [sid, obj, method, args]}
        response = self.http.post("/ubus", json=payload, follow_redirects=False)
        if response.status in (301, 302, 303, 307, 308) and (response.location or "").startswith("https://"):
            # uhttpd's redirect_https: move to HTTPS once and stay there.
            target = urllib.parse.urlsplit(response.location)
            self.base_url = f"https://{target.netloc}"
            self.http = self._client(self.base_url)
            response = self.http.post("/ubus", json=payload, follow_redirects=False)
        if response.status == 404:
            raise RouterError("The router has no web API at /ubus (the uhttpd-mod-ubus package, part of LuCI). Install LuCI or use an SSH key")
        if response.status != 200:
            raise RouterError(f"The router's web API answered with status {response.status}")
        try:
            body = response.json()
        except ProviderError:
            raise RouterError("The router's web API sent an answer Tattle could not read") from None
        if not isinstance(body, dict):
            raise RouterError("The router's web API sent an answer Tattle could not read")
        error = body.get("error")
        if isinstance(error, dict):
            if error.get("code") == JSONRPC_ACCESS_DENIED:
                raise _AccessDenied()
            raise RouterError(f"The router's web API refused the request: {error.get('message') or error.get('code')}")
        result = body.get("result")
        if not isinstance(result, list) or not result:
            raise RouterError("The router's web API sent an answer Tattle could not read")
        return result

    def _login(self) -> str:
        result = self._call(NO_SESSION, "session", "login", {"username": self.user, "password": self.password})
        if result[0] == UBUS_PERMISSION_DENIED:
            raise RouterError("The router refused the user name or password")
        session = result[1].get("ubus_rpc_session") if len(result) > 1 and isinstance(result[1], dict) else None
        if result[0] != 0 or not session:
            raise RouterError(f"The router's sign-in failed (ubus status {result[0]})")
        self.session = session
        return session

    def _exec(self, command: str, args: list[str]) -> list:
        """file exec, signing in again once if the session ran out (rpcd ends
        sessions after five idle minutes, and on restart)."""
        params = {"command": command, "params": args}
        try:
            return self._call(self.session or self._login(), "file", "exec", params)
        except _AccessDenied:
            self.session = None
        try:
            return self._call(self._login(), "file", "exec", params)
        except _AccessDenied:
            raise RouterError(
                "The router account may not run programs through the web API. "
                "Sign in as root, or add Tattle's access rule on the router (see the setup guide)"
            ) from None

    def _web(self) -> str:
        # Tattle's own ACL allows nlbw itself; without it, LuCI's admins may still run
        # luci-app-nlbwmon's helper, which runs the same nlbw command.
        options = [self.command] if self.command else [(NLBW, NLBW_ARGS), (NLBW_ACTION, NLBW_ACTION_ARGS)]
        for command, args in options:
            result = self._exec(command, args)
            if result[0] in (UBUS_PERMISSION_DENIED, UBUS_NOT_FOUND):
                continue
            if result[0] != 0 or len(result) < 2 or not isinstance(result[1], dict):
                raise RouterError(f"The router could not run nlbw (ubus status {result[0]})")
            output = result[1]
            if output.get("code", 0) != 0:
                raise RouterError(_nlbw_failure(str(output.get("stderr") or ""), int(output.get("code") or 1)))
            self.command = (command, args)
            return str(output.get("stdout") or "")
        self.command = None
        raise RouterError(
            "The router did not let this account read nlbwmon. Install luci-app-nlbwmon, "
            "or add Tattle's access rule on the router (see the setup guide)"
        )

    # --- SSH -------------------------------------------------------------------------
    def _ssh(self) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:%-]+", self.host) or self.host.startswith("-"):
            raise RouterError("The router address is not a host name or IP address")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.user) or self.user.startswith("-"):
            raise RouterError("The user name may only have letters, digits, dots, dashes and underscores")
        if not self.ssh_port.isdigit() or not 0 < int(self.ssh_port) < 65536:
            raise RouterError("The SSH port must be a number from 1 to 65535")
        key = key_file_text(self.ssh_key)
        fd, key_path = tempfile.mkstemp(prefix="tattle-ssh-")  # created 0600: ssh refuses keys others can read
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(key)
            command = [
                "ssh", "-F", "none", "-T", "-i", key_path, "-p", self.ssh_port,
                "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={known_hosts_path()}",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10",
                "-o", "LogLevel=ERROR",
                "--", f"{self.user}@{self.host}", " ".join([NLBW, *NLBW_ARGS]),
            ]  # fmt: skip
            try:
                done = subprocess.run(command, capture_output=True, text=True, timeout=SSH_TIMEOUT, check=False)
            except FileNotFoundError:
                raise RouterError("This Tattle install has no ssh program (the openssh-client package)") from None
            except subprocess.TimeoutExpired:
                raise RouterError(f"The router did not answer over SSH within {SSH_TIMEOUT} seconds") from None
        finally:
            os.unlink(key_path)
        if done.returncode == 255:
            raise RouterError(_ssh_failure(done.stderr or "", self.ssh_port))
        if done.returncode != 0:
            raise RouterError(_nlbw_failure(done.stderr or "", done.returncode))
        return done.stdout


class _AccessDenied(RouterError):
    """The web API refused the session (expired) or the call itself. A
    RouterError so that, should it ever escape, it still reads well."""

    def __init__(self) -> None:
        super().__init__("The router's web API refused the session")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no details"


def _ssh_failure(stderr: str, port: str) -> str:
    """ssh exits with 255 for its own errors; say what they mean."""
    if "No user exists for uid" in stderr:
        return "The ssh program cannot run in this Tattle container (its user id has no account); see the OpenWrt setup guide"
    if "Host key verification failed" in stderr or "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr:
        return (
            "The router's SSH identity changed since Tattle first connected. If you reset or replaced the router, "
            f"delete {known_hosts_path()} (it sits next to tattle.db) and test again; otherwise someone may be in between"
        )
    if "Permission denied" in stderr:
        return "The router refused the SSH key: add its public key on the router (see the setup guide)"
    if "Load key" in stderr or "invalid format" in stderr or "passphrase" in stderr:
        return "ssh could not use the pasted key: paste the whole private key, from a key made without a passphrase"
    if "Connection refused" in stderr:
        return f"The router refused SSH connections on port {port}: is SSH (Dropbear) on, and is the port right?"
    return f"Could not reach the router over SSH: {_last_line(stderr)}"


def _nlbw_failure(stderr: str, code: int) -> str:
    if "Error while processing command" in stderr:  # nlbw could not reach the nlbwmon service
        return f"nlbwmon is not running on the router ({_last_line(stderr)}); start it with /etc/init.d/nlbwmon start"
    if code == 127 or "not found" in stderr:
        return "nlbw is not on the router: install the nlbwmon package"
    return f"nlbw failed on the router: {_last_line(stderr)}"
