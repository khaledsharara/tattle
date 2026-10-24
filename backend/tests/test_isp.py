"""The provider service (sign-in, reading, which line is in use, the line page)
driven through the e& provider with a fake e& client. All numbers, accounts
and amounts here are made up; only e&'s field names and structure are real."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tattle import db, isp
from tattle.api import create_app
from tattle.providers import eand_eg
from tattle.providers.eand_eg import EandClient, EandEg, EandError, Tokens, TokenExpired

GB = 1024**3
HOME_A, HOME_B = "0221234567", "0221234568"

PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<getAccountProfileResponse>
  <status>true</status>
  <contracts>
    <contract><subscriberNumber>221234567</subscriberNumber><shdes>EHDSL</shdes><ratePlan>eHome DSL</ratePlan><accountNumber>1.000000001</accountNumber><billCycle>04</billCycle></contract>
    <contract><subscriberNumber>1000000001</subscriberNumber><shdes>MOB</shdes></contract>
  </contracts>
  <linkedDials>
    <contract><subscriberNumber>221234568</subscriberNumber><shdes>EHDSL</shdes></contract>
  </linkedDials>
</getAccountProfileResponse>"""

OPEN_AMOUNT = """<getOpenAmountResponse><status>true</status><advanceAmount>0.0</advanceAmount><bills><bill>
<billDueDate>2026-10-06T00:00:00+03:00</billDueDate><billNumber>1</billNumber><billValue>1234.50</billValue>
</bill></bills><openAmount>1234.50</openAmount></getOpenAmountResponse>"""
BILL_HISTORY = """<getCustomerBillHistoryResponse><status>true</status><customerBillHistory><billItem>
<billDateFrom>2026-08-15T00:00:00+03:00</billDateFrom><billDateTo>2026-09-14T00:00:00+03:00</billDateTo>
<billDueDate>2026-10-06T00:00:00+03:00</billDueDate><billValue>1234.50</billValue>
<invoiceIssueDate>2026-09-15T00:00:00+03:00</invoiceIssueDate><numberOfLines>3</numberOfLines></billItem>
</customerBillHistory></getCustomerBillHistoryResponse>"""


def usage_xml(remaining: str, total: str = "750", unit: str = "GB", extra: str | None = None) -> str:
    """Shaped like e&'s getGenericConsumptionsV2 answer (trimmed, values made up)."""
    plan = f"""<consumption><consumedLabel>eHome DSL</consumedLabel>
        <totalValue>{total}</totalValue><totalUnit>{unit}</totalUnit>
        <remainingValue>{remaining}</remainingValue><remainingUnit>{unit}</remainingUnit></consumption>
      <consumption><totalValue>Unlimited</totalValue><totalUnit>Mins</totalUnit>
        <remainingValue>Unlimited</remainingValue><remainingUnit>Mins</remainingUnit></consumption>"""
    addon = (
        f"""<consumption><consumedLabel>ADSL</consumedLabel><productId>ADSL_Light_Revamp_Service</productId>
        <productName>Valid for 30 days</productName><remainingLabel>ADSL</remainingLabel>
        <remainingUnit>GB</remainingUnit><remainingValue>{extra}</remainingValue></consumption>"""
        if extra is not None
        else ""
    )
    return f"""<getConsumptionResponse xmlns="http://example.invalid/ns">
  <status>true</status>
  <newMeterConsumption><consumptionList>{plan}</consumptionList></newMeterConsumption>
  <ratePlanAddOnConsumption><consumptionList>{addon}</consumptionList></ratePlanAddOnConsumption>
  <ratePlanConsumption>
    <productName>750 GB - 30 Mbps</productName>
    <renewalDate>25/10/2026</renewalDate>
    <consumptionList>{plan}</consumptionList>
  </ratePlanConsumption>
</getConsumptionResponse>"""


def history_xml(days: dict[str, str]) -> str:
    items = "".join(
        f"""<accountHistoryItem><dayDate>{day}</dayDate><historyTransactionsList><historyTransaction>
        <amount>{amount}</amount><callTime>00:00:00</callTime><type>5</type></historyTransaction>
        </historyTransactionsList></accountHistoryItem>"""
        for day, amount in days.items()
    )
    return f"<accountHistoryResponse><status>true</status><accountHistoryList>{items}</accountHistoryList></accountHistoryResponse>"


class FakeEand(EandClient):
    """Answers like e&; `remaining` per line is what the next usage read returns."""

    def __init__(self):
        self.remaining = {HOME_A: "300", HOME_B: "700"}
        self.extra: dict[str, str] = {}
        self.days = {HOME_A: {"2026-10-10": "1024.00 MB"}, HOME_B: {"2026-10-10": "512.00 MB"}}
        self.codes_sent: list[str] = []
        self.bill_reads = 0
        self.access = "a1"
        self.refreshes = 0
        self.refuse_refresh = False

    def send_code(self, number):
        self.codes_sent.append(number)

    def sign_in(self, number, code):
        if code != "1234":
            raise EandError("Invalid code", status=400)
        return Tokens(self.access, "r1")

    def refresh(self, tokens):
        if self.refuse_refresh:
            raise EandError("e& signed Tattle out: send a new code", signed_out=True, status=401)
        self.refreshes += 1
        self.access = f"a{self.refreshes + 1}"
        return Tokens(self.access, f"r{self.refreshes + 1}")

    def _check(self, tokens):
        if tokens.access != self.access:
            raise TokenExpired()

    def profile(self, tokens, number):
        self._check(tokens)
        return PROFILE

    def usage(self, tokens, line):
        self._check(tokens)
        assert line.short_code == "EHDSL"
        return usage_xml(self.remaining[line.number], extra=self.extra.get(line.number))

    def history(self, tokens, line, start, end):
        self._check(tokens)
        return history_xml(self.days[line.number])

    def open_amount(self, tokens, account_number):
        self._check(tokens)
        self.bill_reads += 1
        return OPEN_AMOUNT

    def bill_history(self, tokens, account_number, count=6):
        self._check(tokens)
        return BILL_HISTORY


@pytest.fixture
def eand():
    return FakeEand()


def make_app(settings, eand):
    # Installed as a gateway, so Tattle's count is compared with the provider's.
    counted = replace(settings, mode="gateway")
    return create_app(counted, start_collector=False, provider_instances={"eand_eg": EandEg(client=eand)})


@pytest.fixture
def client(settings, eand):
    with TestClient(make_app(settings, eand)) as c:
        c.post("/api/auth/login", json={"password": "s3cret"})
        yield c


def sign_in(client, number: str = HOME_A, code: str = "1234"):
    started = client.post("/api/isp/sign-in", json={"provider": "eand_eg", "values": {"number": number}})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["challenge"]["kind"] == "code" and number in body["challenge"]["prompt"]
    return client.post("/api/isp/answer", json={"pending": body["pending"], "answer": code})


# --- e&'s answers ---------------------------------------------------------------
def test_parse_usage_reads_data_bundles_and_skips_minutes():
    usage = eand_eg.parse_usage(usage_xml("312.45"))
    assert usage.total == 750 * GB
    assert usage.remaining == int(round(312.45 * GB))
    assert usage.renews == dt.date(2026, 10, 25)
    data = [b for b in usage.bundles if b.is_data]
    assert len(usage.bundles) == 2 and len(data) == 1  # the repeated newMeterConsumption is not counted twice
    assert data[0].name == "750 GB - 30 Mbps" and data[0].total_bytes - data[0].remaining_bytes == int(round(437.55 * GB))


def test_extra_bundle_counts_toward_what_is_left_without_a_size():
    usage = eand_eg.parse_usage(usage_xml("0", total="750.0", unit="MB", extra="40.5"))
    extra = next(b for b in usage.bundles if b.kind == "extra")
    assert extra.name == "Valid for 30 days" and extra.remaining_bytes == int(round(40.5 * GB))
    assert extra.total_bytes is None and extra.is_data
    assert usage.remaining == int(round(40.5 * GB))
    assert usage.renews == dt.date(2026, 10, 25)  # the plan's renewal, not the extra bundle's


def test_history_adds_up_internet_per_day():
    days = eand_eg.parse_history(history_xml({"2026-10-10": "1500.25 MB", "2026-10-11": "300.50 MB"}))
    assert days == {"2026-10-10": int(round(1500.25 * 1024**2)), "2026-10-11": int(round(300.50 * 1024**2))}


def test_parse_usage_raises_e_and_message_on_fault():
    fault = "<getConsumptionResponse><status>false</status><fault><userMessageEn>Service unavailable</userMessageEn></fault></getConsumptionResponse>"
    with pytest.raises(EandError, match="Service unavailable"):
        eand_eg.parse_usage(fault)


def test_profile_lists_home_lines_and_linked_dials():
    lines = [line for line in eand_eg.parse_lines(PROFILE) if eand_eg.is_home_line(line.number)]
    assert [line.number for line in lines] == [HOME_A, HOME_B]


def test_numbers_and_router_logins():
    assert eand_eg.normalise_number("02 2123 4567") == HOME_A
    with pytest.raises(ValueError):
        eand_eg.normalise_number("221234567")
    assert EandEg.match_wan_login("ETIS_0221234567", [HOME_B, HOME_A]) == HOME_A
    assert EandEg.match_wan_login("someone@isp", [HOME_A]) is None
    assert eand_eg.to_bytes("1.5", "Giga Byte") == int(1.5 * GB)
    assert eand_eg.to_bytes("Unlimited", "GB") is None


def test_bills():
    bills = eand_eg.parse_bills(OPEN_AMOUNT, BILL_HISTORY)
    assert bills["open_amount"] == 1234.50
    assert bills["unpaid"] == [{"due": "2026-10-06", "value": 1234.50}]
    assert bills["bills"][0] == {
        "from": "2026-08-15", "to": "2026-09-14", "issued": "2026-09-15", "due": "2026-10-06", "value": 1234.50, "lines": 3
    }


def test_renewal_dates():
    assert isp.renewal_date("25/10/2026") == dt.date(2026, 10, 25)
    assert isp.renewal_date("2026-10-25") == dt.date(2026, 10, 25)
    assert isp._month_before(dt.date(2026, 3, 31)) == dt.date(2026, 2, 28)


# --- signing in and reading -------------------------------------------------------
def test_sign_in_reads_every_line_and_marks_the_live_one(client, settings, eand):
    conn = db.connect(settings.db_path)
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    first = client.post("/api/isp/sign-in", json={"provider": "eand_eg", "values": {"number": HOME_A}})
    assert eand.codes_sent == [HOME_A]
    # A second code within a minute is refused instead of texting again.
    again = client.post("/api/isp/sign-in", json={"provider": "eand_eg", "values": {"number": HOME_A}})
    assert again.status_code == 502

    pending = first.json()["pending"]
    wrong = client.post("/api/isp/answer", json={"pending": pending, "answer": "9999"})
    assert wrong.status_code == 502 and wrong.json()["detail"] == "Invalid code"
    # The mistyped code can be typed again.
    assert client.post("/api/isp/answer", json={"pending": pending, "answer": " 12 34 "}).json() == {"done": True}

    data = client.get("/api/isp").json()
    assert data["live_number"] == HOME_A and data["live_source"] == "router"
    lines = {line["number"]: line for line in data["lines"]}
    assert set(lines) == {HOME_A, HOME_B}
    assert lines[HOME_A]["live"] and not lines[HOME_B]["live"]
    assert lines[HOME_A]["reading"]["remaining"] == 300 * GB
    assert lines[HOME_A]["provider"]["name"] == "e& (Etisalat) Egypt" and lines[HOME_A]["provider"]["unit_base"] == 1024
    assert data["accounts"][0]["identifier"] == HOME_A


def test_sign_in_needs_the_fields(client):
    r = client.post("/api/isp/sign-in", json={"provider": "eand_eg", "values": {}})
    assert r.status_code == 400 and "Home landline number" in r.json()["detail"]
    assert client.post("/api/isp/sign-in", json={"provider": "nope", "values": {}}).status_code == 502


def test_check_compares_the_provider_with_tattle_while_the_line_is_live(client, settings, eand):
    conn = db.connect(settings.db_path)
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    conn.execute("INSERT INTO counter_state(name, value) VALUES('attributed_rx', 1000), ('attributed_tx', 0)")
    sign_in(client)
    conn.execute("UPDATE isp_readings SET ts = ts - 900")

    eand.remaining[HOME_A] = "299"  # 1 GB more used according to e&
    conn.execute("UPDATE counter_state SET value = value + ? WHERE name = 'attributed_rx'", (GB - 5_000_000,))
    data = client.post("/api/isp/read").json()
    line = next(line for line in data["lines"] if line["number"] == HOME_A)
    assert line["check"]["isp"] == GB
    assert line["check"]["counted"] == GB - 5_000_000
    other = next(line for line in data["lines"] if line["number"] == HOME_B)
    assert other["check"] is None  # not the line in use

    # The house switches the router to the other line: the comparison starts over there.
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234568")
    data = client.post("/api/isp/read").json()
    assert next(line for line in data["lines"] if line["number"] == HOME_B)["check"] is None
    assert next(line for line in data["lines"] if line["number"] == HOME_A)["check"] is None


def test_without_a_router_the_line_whose_usage_moves_is_live(client, settings, eand):
    conn = db.connect(settings.db_path)
    sign_in(client)
    assert client.get("/api/isp").json()["live_number"] is None  # two lines, no evidence yet
    conn.execute("UPDATE isp_readings SET ts = ts - 900")
    eand.remaining[HOME_B] = "690"  # 10 GB used on B, A untouched
    data = client.post("/api/isp/read").json()
    assert (data["live_number"], data["live_source"]) == (HOME_B, "usage")
    assert conn.execute("SELECT number, source FROM line_switches").fetchall()[-1][:] == (HOME_B, "usage")

    # Saying it by hand works too, and evidence takes over again later.
    assert client.put("/api/isp/live", json={"number": HOME_A}).json()["live_number"] == HOME_A
    assert client.put("/api/isp/live", json={"number": "0299999999"}).status_code == 404


def test_check_follows_extra_bundles_and_restarts_when_one_is_bought(client, settings, eand):
    conn = db.connect(settings.db_path)
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    conn.execute("INSERT INTO counter_state(name, value) VALUES('attributed_rx', 0), ('attributed_tx', 0)")
    eand.remaining[HOME_A] = "0"
    sign_in(client)
    conn.execute("UPDATE isp_readings SET ts = ts - 1800")

    eand.extra[HOME_A] = "100"  # a 100 GB bundle bought: what is left goes up
    data = client.post("/api/isp/read").json()
    line = next(line for line in data["lines"] if line["number"] == HOME_A)
    assert line["check"] is None and line["reading"]["remaining"] == 100 * GB
    conn.execute("UPDATE isp_readings SET ts = ts - 900 WHERE ts > (SELECT MIN(ts) FROM isp_readings)")

    eand.extra[HOME_A] = "98"  # 2 GB of the bundle used
    conn.execute("UPDATE counter_state SET value = ? WHERE name = 'attributed_rx'", (2 * GB,))
    line = next(line for line in client.post("/api/isp/read").json()["lines"] if line["number"] == HOME_A)
    assert line["check"]["isp"] == 2 * GB and line["check"]["counted"] == 2 * GB


def test_days_add_up_every_line_next_to_tattle(client, settings, eand):
    from zoneinfo import ZoneInfo

    cairo = ZoneInfo("Africa/Cairo")
    noon = dt.datetime(2026, 10, 11, 12, tzinfo=cairo).timestamp()
    conn = db.connect(settings.db_path)
    sign_in(client)
    oct10 = int(dt.datetime(2026, 10, 10, tzinfo=cairo).timestamp())
    conn.execute("INSERT INTO devices(id, key, first_seen, last_seen) VALUES(500, 'mac:x', 0, 0)")
    conn.execute("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(500, ?, ?, 0)", (oct10 - 3600, 7))
    conn.execute("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(500, ?, ?, ?)", (oct10 + 3600, 1000, 24))

    days = isp.daily_comparison(conn, cairo, noon)
    oct10_row = next(d for d in days if d["day"] == "2026-10-10")
    assert oct10_row["isp"] == 1536 * 1024**2  # both lines: 1024 MB + 512 MB
    assert oct10_row["by_line"] == {HOME_A: 1024 * 1024**2, HOME_B: 512 * 1024**2}
    assert oct10_row["counted"] == 1024 and oct10_row["complete"] is True
    today = next(d for d in days if d["day"] == "2026-10-11")
    assert today["complete"] is False and today["isp"] is None


def test_expired_token_is_refreshed_and_a_refused_refresh_asks_to_sign_in_again(client, settings, eand):
    sign_in(client)
    eand.access = "rotated-by-provider"
    client.post("/api/isp/read")
    assert eand.refreshes == 1
    assert client.get("/api/isp").json()["accounts"][0]["status"] == "ok"
    conn = db.connect(settings.db_path)
    saved = json.loads(conn.execute("SELECT credentials FROM provider_accounts").fetchone()[0])
    assert saved["access"] == eand.access  # the rotated token is kept

    eand.access = "rotated-again"
    eand.refuse_refresh = True
    data = client.post("/api/isp/read").json()
    assert data["accounts"][0]["status"] == "expired"
    health = client.get("/api/health").json()
    assert next(c for c in health["checks"] if c["id"] == "isp")["ok"] is False


def test_only_admin_can_sign_in_and_remove(settings, eand):
    with TestClient(make_app(settings, eand)) as anonymous:
        assert anonymous.post("/api/isp/sign-in", json={"provider": "eand_eg", "values": {"number": HOME_A}}).status_code == 401
        assert anonymous.get("/api/isp").status_code == 200
        assert anonymous.get("/api/providers").json()["providers"][0]["id"] == "eand_eg"


def test_remove_forgets_the_sign_in(client, eand):
    sign_in(client)
    account_id = client.get("/api/isp").json()["accounts"][0]["id"]
    data = client.delete(f"/api/isp/accounts/{account_id}").json()
    assert data["accounts"] == [] and data["lines"] == []


def test_line_page_spans_bills_pace_and_comparable_days(client, settings, eand):
    conn = db.connect(settings.db_path)
    db.set_setting(conn, isp.ROUTER_LOGIN_SETTING, "ETIS_0221234567")
    db.set_setting(conn, isp.ROUTER_LINK_SETTING, '{"state": "up", "up_kbps": 5000, "down_kbps": 30000, "ts": 1}')
    today = dt.date.today()
    days = {(today - dt.timedelta(days=back)).isoformat(): "10240.00 MB" for back in range(1, 8)}
    days[(today - dt.timedelta(days=3)).isoformat()] = "20480.00 MB"
    eand.days = {HOME_A: days, HOME_B: {(today - dt.timedelta(days=9)).isoformat(): "1024.00 MB"}}
    sign_in(client)
    client.post("/api/isp/read")
    assert eand.bill_reads == 1  # bills are read every few hours, not every 15 minutes

    # Tattle counted the house all of yesterday.
    yesterday = today - dt.timedelta(days=1)
    start, _ = isp._day_bounds(yesterday, settings.timezone)
    conn.execute("INSERT INTO devices(id, key, first_seen, last_seen) VALUES(700, 'mac:z', 0, 0)")
    conn.execute("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(700, ?, ?, 0)", (start - 3600, 1))
    conn.execute("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(700, ?, ?, 0)", (start + 3600, 9 * GB))

    page = client.get(f"/api/isp/lines/{HOME_A}?span=7d").json()
    line = page["line"]
    assert line["live"] and line["in_use_since"] == (today - dt.timedelta(days=7)).isoformat()
    assert line["pace"]["per_day"] == int(80 * GB / 7)  # the last 7 full days: six of 10 GB, one of 20 GB
    assert line["pace"]["runs_out"] is not None
    assert page["range"]["from"] == (today - dt.timedelta(days=6)).isoformat()
    by_day = {d["day"]: d for d in page["days"]}
    assert by_day[yesterday.isoformat()]["isp"] == 10 * GB
    assert by_day[yesterday.isoformat()]["counted"] == 9 * GB
    assert by_day[today.isoformat()]["isp"] == 0  # no use listed yet today
    assert page["totals"]["busiest"]["isp"] == 20 * GB
    assert page["billing"]["open_amount"] == 1234.50 and page["billing"]["lines_on_account"] == 3
    assert page["billing"]["bill_cycle"] == "04"
    assert page["link"]["down_kbps"] == 30000
    assert page["cycle"] == {"start": "2026-09-25", "end": "2026-10-24"}

    everything = client.get(f"/api/isp/lines/{HOME_A}?span=all").json()
    assert everything["range"]["from"] == everything["range"]["earliest"]
    custom = client.get(f"/api/isp/lines/{HOME_A}?span=custom&from={today - dt.timedelta(days=2)}&to={today}").json()
    assert len(custom["days"]) == 3
    assert client.get(f"/api/isp/lines/{HOME_A}?span=custom&from=2026-10-05&to=2026-10-01").status_code == 400
    assert client.get("/api/isp/lines/0299999999").status_code == 404
    other = client.get(f"/api/isp/lines/{HOME_B}").json()
    assert other["link"] is None and other["line"]["live"] is False


def test_databases_from_before_pluggable_providers_keep_their_sign_ins(tmp_path):
    path = str(tmp_path / "old.db")
    conn = db.connect(path)
    conn.executescript(
        """
        CREATE TABLE isp_logins (id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL);
        CREATE TABLE isp_lines (number TEXT PRIMARY KEY, login_id INTEGER NOT NULL REFERENCES isp_logins(id) ON DELETE CASCADE,
            short_code TEXT, plan TEXT, error TEXT, read_at INTEGER, account_number TEXT, bill_cycle TEXT,
            billing TEXT, billing_at INTEGER);
        INSERT INTO isp_logins VALUES (7, '0221234567', 'acc', 'ref', 'ok', NULL, 1, 2);
        INSERT INTO isp_lines VALUES ('0221234567', 7, 'EHDSL', 'eHome DSL', NULL, 3, '1.000000001', '04', NULL, NULL);
        """
    )
    conn.close()
    db.init(path)
    conn = db.connect(path)
    account = conn.execute("SELECT * FROM provider_accounts").fetchone()
    assert (account["id"], account["provider"], account["identifier"]) == (7, "eand_eg", "0221234567")
    assert json.loads(account["credentials"]) == {"number": "0221234567", "access": "acc", "refresh": "ref"}
    line = conn.execute("SELECT * FROM isp_lines").fetchone()
    assert (line["login_id"], line["provider"], line["account_number"]) == (7, "eand_eg", "1.000000001")
    assert json.loads(line["meta"]) == {"short_code": "EHDSL", "bill_cycle": "04"}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "isp_logins" not in tables
