# Adding a router adapter

A router adapter teaches Tattle to read one kind of router: per-device traffic
counters (which give *router mode*: counting per device without becoming the
network's gateway), the router's own internet counters, the internet login,
the line's status, or whether its DHCP server is on.

The interface is [`base.py`](../backend/tattle/routers/base.py). Working
examples to copy from:

- [`dlink_dsl245ge.py`](../backend/tattle/routers/dlink_dsl245ge.py): a web UI
  read with regexes, sign-in tracked per IP (stable).
- [`mikrotik_rest.py`](../backend/tattle/routers/mikrotik_rest.py): a JSON REST
  API with Basic auth.
- [`unifi.py`](../backend/tattle/routers/unifi.py): two sign-in styles, a
  session with a CSRF token, signing in again when it runs out.
- [`openwrt_nlbwmon.py`](../backend/tattle/routers/openwrt_nlbwmon.py): a
  JSON-RPC API, and SSH through the system `ssh` client.

## 1. The contract

Subclass `RouterAdapter` and set its class attributes:

```python
class MyRouter(RouterAdapter):
    id = "myrouter"                 # unique, lower case; the file is <id>.py
    name = "My Router (firmware 2)" # as people know it
    status = "beta"                 # new adapters start here
    capabilities = frozenset({"device_counters", "wan_counters"})
    fields = (
        Field("url", "Router address", placeholder="https://192.168.1.1"),
        Field("user", "User name"),
        Field("password", "Password", kind="password"),
    )
    notes = "One or two sentences under the form: what it reads, what it can't."
    docs = "docs/routers/myrouter.md"
    device_modulus = None           # 2**32 if per-device counters wrap
    wan_modulus = None              # 2**32 if the internet counters wrap
    poll_seconds = 60               # how often device counters are worth reading

    def __init__(self, values: dict[str, str], transport: Transport | None = None):
        super().__init__(values)
        ...
```

Implement the methods for each capability you declare:

| Capability | Method | Returns |
| --- | --- | --- |
| `device_counters` | `device_counters()` | `list[DeviceCounter]`, running totals per MAC |
| `wan_counters` | `wan_counters()` | `(received, sent)` bytes on the internet side |
| `wan_login` | `wan_login()` | the PPPoE user name, or `None` |
| `link_status` | `link_status()` | `{"state": "up", "down_kbps": int\|None, "up_kbps": int\|None}` |
| `dhcp_enabled` | `dhcp_enabled()` | `True` when the router's DHCP server is on |

Declare a capability only when every router of that kind can answer it: the
collector calls the method on every reading, and **Save and test** calls them
all. If some routers can't, leave the capability out (UniFi has no WAN totals,
so it declares only `device_counters`).

`fields` are the inputs Settings shows. Kinds: `text`, `password`, `tel`,
`email`; mark optional ones `optional=True`. Password fields are never sent
back to the browser. A password box drops line breaks from pasted text, so
anything multi-line (an SSH key) must be rebuilt (see `key_file_text` in
`openwrt_nlbwmon.py`). For yes/no settings, a text field that reads
`yes`/`no` is fine.

## 2. `DeviceCounter` and direction

```python
DeviceCounter(mac, rx, tx, ip=None, hostname=None)
```

- `mac`: the device's MAC address (Tattle lower-cases it).
- **`rx`: bytes the device downloaded. `tx`: bytes it uploaded.** These are
  from the *device's* point of view. Routers and access points often count
  from their own side, where "tx" is what they sent *to* the device, which is
  the device's download. Get this right from the router's source, its own UI,
  or a test with a known download, and **say in the doc page how you know**.
- `ip`, `hostname`: what the router knows, if anything. Prefer a stable IPv4
  address over IPv6 privacy addresses.

The totals are running totals as the router keeps them. Every `poll_seconds`
Tattle books the difference since the last reading:

- the first reading is only a starting point;
- a total that goes **down** is a restart (reboot, reconnect, new period), and
  the new value counts as new traffic, unless `device_modulus` says the
  counter wraps and it only wrapped;
- a jump larger than the line could carry since the last reading is skipped;
- a device missing from a reading keeps its last totals.

So never subtract, reset or cache counters yourself; return what the router
says. If a device has several rows (one per IP address, say), add them up per
MAC so the total stays monotonic. Skip a device whose counters you can't read
rather than returning 0 for it (a 0 looks like a restart).

**Internet only, or everything?** Tattle tracks a quota, and traffic that stays
in the house doesn't use it. Where the router lets you, count internet traffic
only. Where it doesn't (access points count everything), say so clearly in the
doc page and in the README's adapter table.

## 3. Errors

Raise `RouterError` with a sentence a non-technical person can act on: "The
router refused the user name or password", not "HTTP 401". The message is
shown in Settings and on the dashboard as it is.

- Pass `who="the router"` and `error=RouterError` to `Http`, so network
  failures become "Could not reach the router: ...".
- The collector and **Save and test** catch only `RouterError`, `OSError` and
  `ValueError`. Turn everything else into a `RouterError`: JSON that isn't
  what you expected, `subprocess.TimeoutExpired`, a missing program.
- Never put a secret in a message (a password, a key, a token).

## 4. Sessions

Tattle creates the adapter once and reuses it for every reading, so keep a
session (cookie, token) on the instance. When the router says the session ran
out, sign in again **once** and retry; if that fails too, raise. Don't loop,
and don't mistake other refusals for an expired session (UniFi answers a wrong
site with 401, the same status as an expired session).

`test()` (the **Save and test** button) calls every capability's method and
returns one line per finding. Override it to add a line, e.g. which way the
adapter signed in, and call `super().test()` for the rest.

## 5. Talking to the router

**HTTP:** use `Http` from [`providers/http.py`](../backend/tattle/providers/http.py):
standard library only, a cookie jar, JSON or form bodies,
`follow_redirects=False`, and a `transport` hook for tests. Routers usually
have self-signed certificates: pass `verify_tls=False` by default and offer a
"Check the certificate" field for people who installed a proper one.

**SSH:** use the system `ssh` client through `subprocess.run` (see
`openwrt_nlbwmon.py`):

- write the key to a file from `tempfile.mkstemp` (mode 0600) and delete it in
  `finally`;
- pass `-F none -T -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<file next to
  tattle.db>` and a timeout;
- check the host, user and port against strict patterns and put `--` before
  `user@host`, so a value can never become an ssh option;
- prefer a key the router restricts to one command.

**No new dependencies.** Standard library only.

## 6. Tests

Put them in `backend/tests/test_router_<id>.py`. Fake the router: a
`transport` (a function `Request -> Response`) for HTTP, or
`monkeypatch.setattr(module.subprocess, "run", fake)` for SSH. Build the fake
answers in the shape the real router uses, and say in the module docstring
where the shape comes from (source code, the router's manual, a capture you
scrubbed).

Cover at least:

- parsing, with values in the forms the router really uses (strings for
  numbers, upper-case MACs, missing fields);
- **direction**: a device's download ends up in `rx`;
- signing in, and signing in again once when the session runs out (and not
  looping when it keeps failing);
- wrong credentials, a refused permission, and an unreachable host
  (`127.0.0.1:1` gives a real "connection refused" quickly);
- what `test()` returns.

Run:

```
cd backend && python -m pytest -q -p no:cacheprovider tests/test_router_<id>.py
python -m pyflakes tattle/routers/
```

**Testing without the hardware.** Real router software runs on a PC:
OpenWrt publishes container images (`openwrt/rootfs`), MikroTik a free
RouterOS VM image (CHR), and Ubiquiti's UniFi Network Application runs in a
container. A second container or VM routed through the router lets you check
direction with a known download. It isn't real hardware, so the adapter stays
`beta`, but it turns guesses into facts.

## 7. Register it

Add the module name to `MODULES` in
[`backend/tattle/routers/__init__.py`](../backend/tattle/routers/__init__.py),
and touch nothing else in that file. A module that fails to import is skipped
and logged, so a broken adapter can't take the dashboard down.

## 8. The doc page

Add `docs/routers/<id>.md` and a row in the table in
[`docs/routers/README.md`](routers/README.md). Say:

- the status (beta: untested on real hardware, testers wanted);
- what it reads, and **whether the counters include traffic that stays in
  the house**;
- what to set up on the router, with a least-privilege account (read-only
  user, an ACL, a restricted key);
- the fields;
- what is **verified** (and how) and what is **unverified**;
- known limits and references;
- the test-report link:
  `https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml`.

## Privacy rule (important: this is public)

Never put real data in the repo: not in tests, docs or comments. Router dumps
are full of it. Scrub before you copy anything.

- **MAC addresses:** use the documentation range from RFC 7042,
  `00:00:5e:00:53:00` to `00:00:5e:00:53:ff`.
- **IP addresses:** `192.0.2.x` (documentation range), or generic private
  examples like `192.168.1.20`. Never a public address.
- **Names:** made up, like `living-room-tv` or `maya-iphone`. No real names,
  SSIDs or serial numbers.
- **Logins:** PPPoE user names often carry a phone or account number. Use
  obviously fake ones (`ETIS_0221234567`, `example-user@isp`).
- **Secrets:** no real passwords, keys or tokens, not even "old" ones. Make
  test keys at test time (see `test_rebuilt_real_keys_still_load`).

Keep only field names and structure: they are what makes the adapter work.

## PR checklist

- [ ] `backend/tattle/routers/<id>.py`: subclasses `RouterAdapter`, standard
      library only, type hints, short docstrings that say *why*.
- [ ] `rx` is the device's download, and the doc page says how you know.
- [ ] Every failure is a `RouterError` with a plain sentence, no secrets.
- [ ] Sessions are kept and renewed once, never in a loop.
- [ ] `device_modulus` / `wan_modulus` set if the counters wrap.
- [ ] `backend/tests/test_router_<id>.py`: a fake transport (or fake
      `subprocess.run`), made-up data, the cases in section 6.
- [ ] `docs/routers/<id>.md` and a row in `docs/routers/README.md`.
- [ ] Added to `MODULES`, nothing else changed in `__init__.py`.
- [ ] `pytest` and `pyflakes` clean.
- [ ] No real data anywhere.
