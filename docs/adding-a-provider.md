# Adding an internet-provider plugin

A provider plugin teaches Tattle to read one ISP's home-internet usage the way
that ISP's own website or app does: sign in like a customer, list the home lines
on the account, and read each line's bundles (plan + extras), the renewal date,
and — where the ISP offers them — a daily history and the bills.

This guide walks through writing one. The working example to copy from is
[`backend/tattle/providers/eand_eg.py`](../backend/tattle/providers/eand_eg.py)
(e& Egypt — the one `stable` provider). The interface it implements is
[`base.py`](../backend/tattle/providers/base.py); the little HTTP client is
[`http.py`](../backend/tattle/providers/http.py).

## 1. The contract

Tattle talks to your provider through a handful of methods. You subclass
`Provider` and set some class attributes:

```python
class MyIsp(Provider):
    id = "myisp_xx"          # unique, lowercase; the file is named <id>.py
    name = "My ISP"          # as customers know it
    country = "XX"           # ISO 3166, e.g. "EG", "AU", "BE"
    status = "beta"          # new providers start here
    unit_base = 1000         # or 1024 — see "Units" below
    currency = "USD"         # or None
    fields = (...)           # the sign-in form (see below)
    notes = "One or two sentences shown under the sign-in form."
    docs = "docs/providers/myisp_xx.md"
    has_daily = False        # set True if you implement daily()
    has_billing = False      # set True if you implement billing()
```

### `fields` — the sign-in form

Each `Field` is one input Settings shows. Kinds: `text`, `password`, `tel`,
`email`. Mark optional inputs `optional=True`.

```python
fields = (
    Field("number", "Landline number", kind="tel", placeholder="0221234567",
          help="The landline your home internet is on."),
    Field("password", "Password", kind="password"),
)
```

### How Tattle uses your methods

1. **`start_sign_in(values)`** — `values` is what the person typed. Return either
   the **credentials** (a plain JSON-able `dict` Tattle stores on the server) or
   a **`Challenge`** when the ISP needs another step.
2. **`answer(values, challenge, answer)`** — called with what the person typed
   for the challenge. Return credentials or another `Challenge`.
3. **`refresh(credentials)`** — called once, after a `SessionExpired`, to get
   fresh credentials. Return the new dict. Raise
   `ProviderError(signed_out=True)` when the person must sign in again.
4. **`lines(credentials)`** — the home lines on the account, as `Line`s.
5. **`usage(credentials, line)`** — the current `Usage` for one line.
6. **`daily(credentials, line, start, end)`** — optional; `{"2026-10-10": bytes}`.
7. **`billing(credentials, line)`** — optional; a bills dict (see `base.py`).
8. **`match_wan_login(login, line_ids)`** — optional; which line a router's PPPoE
   user name belongs to, if it says.

Every ~15 minutes Tattle calls `lines()` then `usage()` for each line (and
`daily()` / `billing()` when `has_daily` / `has_billing`). If any call raises
`SessionExpired`, Tattle calls `refresh()` once, saves the result, and retries.

### What the errors mean to Tattle

- `SessionExpired` → "my session ran out; please `refresh()` and retry". It is a
  `ProviderError` with `signed_out=True`.
- `ProviderError(signed_out=True)` → Settings marks the account "signed out" and
  asks the person to sign in again.
- any other `ProviderError` → shown to the person and retried next round.

**Messages are read by non-technical people.** Write plain, specific sentences:
"We did not accept that password" — not "HTTP 401". A good pattern is a private
`Rejected(ProviderError)` subclass for "the ISP turned the credentials down", so
`refresh()` can turn it into `signed_out=True` while network hiccups just retry.

## 2. The data types

- **`Line(id, plan=None, account=None, meta={})`** — one home line. `id` is
  stable (a landline, a service id). `meta` is yours to keep for later calls.
- **`Bundle(kind, name, total_bytes, remaining_bytes, renews=None, label=None, is_data=True)`**
  — a plan (`kind="plan"`) or an extra (`kind="extra"`). **Sizes are in bytes**;
  use `None` when unknown (e.g. unlimited plans, or extras where the ISP only
  says what's left). Set `is_data=False` for minutes/SMS buckets so they aren't
  added into the data total.
- **`Usage(bundles, renews=None, cycle_start=None)`** — `renews` is the date the
  quota renews next (the cycle's end); `cycle_start` when the ISP tells you.
  `Usage.total`, `.remaining` and `.data` are computed from the data bundles.

## 3. The Challenge flow

A `Challenge` is a step the person answers before sign-in finishes:

- `kind="code"` — type a code the ISP sent (SMS/e-mail/app). See `eand_eg.py`.
- `kind="captcha"` — type the characters in `image` (a `data:` URL). See
  `we_eg.py`.
- `kind="paste"` — open `url`, sign in there, paste back where the page lands.
  See `xfinity_us.py`.

`state` is a dict that stays on the server between the two steps and is **never**
sent to the browser — put tokens, cookies, a code_verifier there.

```python
def start_sign_in(self, values):
    ...  # ask the ISP to send a code / make a captcha
    return Challenge("code", "We texted a code to the account. Type it here.",
                     state={"number": number})

def answer(self, values, challenge, answer):
    number = challenge.state["number"]
    ...  # exchange the code for tokens
    return {"number": number, "access": ..., "refresh": ...}
```

## 4. Credentials and refresh

Credentials are whatever JSON-able dict you need back on every call: tokens,
cookies (`http.export_cookies()`), the account number. Tattle saves the dict
after each round, **including in-place changes** — so rotating a token mid-read
is fine; just mutate the dict (or return a new one from `refresh()`).

For password-based ISPs it's acceptable to keep the username and password in the
credentials so `refresh()` can sign in again silently. Tattle is self-hosted and
this is documented to the person; still, prefer a refresh token when the ISP
offers one, and only fall back to re-login.

## 5. Units

Return bytes using the ISP's **real** unit base:

- `unit_base = 1000` — the ISP counts 1 GB = 1000 MB (decimal).
- `unit_base = 1024` — the ISP counts 1 GB = 1024 MB (binary).

Find out which by reading the ISP's app/site or a reference project (e.g. e&
reports a used-up 750 GB plan's daily history as ~768,000 MB → 1024). If you
genuinely can't tell, use `1000` and **say so in your doc page** so a tester can
confirm. Convert with a small helper, e.g.:

```python
def _bytes(value, power):   # power: KB=1, MB=2, GB=3
    return int(round(float(value) * self.unit_base ** power))
```

## 6. The HTTP client

Use `Http` from `http.py` — standard library only, with a cookie jar, JSON/form
bodies, `follow_redirects=False` for sign-in redirects, cookie export/import, and
a **`transport` hook for tests**. Create it per call (or per sign-in) and pass
`who=` and `error=` so network failures become your `ProviderError`:

```python
def _http(self):
    return Http(BASE_URL, transport=self.transport, who="My ISP", error=MyError)
```

Your provider's `__init__` should accept `transport=None` and store it, so tests
can inject a fake network (see below). Keep to the standard library — **no new
dependencies.** If you must parse HTML, use `html.parser` (see `ogero_lb.py`) or
careful regex, not a third-party parser.

## 7. Tests with a fake transport

A `transport` is a function `Request -> Response` (both from `http.py`). Your
test builds canned responses shaped like the ones you found in the reference
projects, and drives the whole flow. **Never put real account data in tests** —
see the privacy rule below.

```python
from tattle.providers.http import Response
from tattle.providers.myisp_xx import MyIsp

def _json(payload, status=200):
    import json
    return Response(status, {"content-type": "application/json"}, json.dumps(payload), "https://isp/x")

class FakeIsp:
    def __init__(self): self.session_valid = True
    def __call__(self, request):
        if request.url.endswith("/login"):
            return _json({"access": "a", "refresh": "r"})
        if "/usage" in request.url:
            if not self.session_valid:
                return _json({}, status=401)   # -> SessionExpired
            return _json({"total": 100, "used": 40})
        raise AssertionError(request.url)

def test_sign_in_and_usage():
    provider = MyIsp(transport=FakeIsp())
    creds = provider.start_sign_in({"number": "0221234567", "password": "pw"})
    line = provider.lines(creds)[0]
    usage = provider.usage(creds, line)
    assert usage.remaining is not None
```

Cover, at least:

- sign-in (including the Challenge step if any),
- `refresh()` after a `SessionExpired`, and the retry succeeding,
- `lines()` and `usage()` → bundles with the right **bytes** and `renews`,
- `daily()` / `billing()` if you implement them,
- error paths: a wrong password → `ProviderError` with a friendly message and
  `signed_out` **False**; a dead session → `signed_out` **True**.

Put the file at `backend/tests/test_provider_<id>.py` and run:

```
cd backend && python -m pytest -q -p no:cacheprovider tests/test_provider_<id>.py
python -m pyflakes tattle/providers/
python -m ruff check tattle/providers/ --select E,F,W,B --ignore E501
```

## 8. Register it

Add your module name to `MODULES` in
[`backend/tattle/providers/__init__.py`](../backend/tattle/providers/__init__.py)
— and touch nothing else in that file:

```python
MODULES = [
    "eand_eg",
    ...
    "myisp_xx",
]
```

A broken plugin is skipped at load time, so a mistake in yours won't take the
dashboard down — but run the tests and linters anyway.

## 9. Write the doc page

Add `docs/providers/<id>.md` with: the status (beta, needs testers), what it
reads, how sign-in works (what the person sees), what's **verified vs
unverified**, known limits (captcha, unlimited plans, rate limits), references
(links to what you read), and a line pointing testers to
`https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml`.

## Privacy rule (important — this is public)

Never put real account data anywhere in the repo — not in tests, not in docs,
not in comments. No real phone numbers, account numbers, names, e-mails,
addresses, tokens, or amounts. Use obviously fake values: landline `0221234567`,
user `user@example.com`, account `ACC-0001`. Keep only the ISP's **field names
and structure**, which are what make the plugin work.

## PR checklist

- [ ] `backend/tattle/providers/<id>.py` — subclasses `Provider`, standard
      library only, concise docstrings explaining *why*, type hints.
- [ ] `unit_base` set to the ISP's real base (or `1000` with a note in the doc).
- [ ] Error messages are plain sentences for non-technical people.
- [ ] `backend/tests/test_provider_<id>.py` — full flow against a fake
      `transport`, incl. refresh and error paths; all made-up data.
- [ ] `docs/providers/<id>.md` — verified vs unverified, limits, references,
      test-report link.
- [ ] Added to `MODULES` (and nothing else changed in `__init__.py`).
- [ ] `pytest`, `pyflakes` and `ruff` all clean.
- [ ] No real account data anywhere.
- [ ] Credited the reference projects in the doc; did not copy their code.
