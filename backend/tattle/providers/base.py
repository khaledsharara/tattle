"""What an internet provider plugin implements.

A provider reads a home internet line's quota the way the provider's own
customer website or app does: it signs in like a customer, lists the home
lines on the account, and reads each line's bundles (plan and extras), the
renewal date, and, where the provider offers them, daily use and bills.

Sign-in can take more than one step (a code sent by SMS, a captcha, a code
pasted from a browser login): `start_sign_in` returns either the credentials
or a `Challenge`, and `answer` finishes it. Credentials are a JSON-able dict
that Tattle stores on the server and hands back on every call; a provider may
update it in place (new tokens, cookies) and Tattle saves the change.

See docs/adding-a-provider.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import ClassVar, Literal

Status = Literal["stable", "beta"]


class ProviderError(Exception):
    """Something the provider refused or could not answer; the message is shown
    to people as is, so write it for them."""

    def __init__(self, message: str, *, signed_out: bool = False, status: int | None = None):
        super().__init__(message)
        self.signed_out = signed_out  # the sign-in is no good any more: ask the person again
        self.status = status  # the provider's HTTP status, None when it could not be reached


class SessionExpired(ProviderError):
    """The session ran out; Tattle calls `refresh` once and tries again."""

    def __init__(self, message: str = "The session ran out") -> None:
        super().__init__(message, signed_out=True)


@dataclass(frozen=True)
class Field:
    """One input on the sign-in form."""

    name: str
    label: str
    kind: Literal["text", "password", "tel", "email"] = "text"
    placeholder: str = ""
    help: str = ""
    optional: bool = False


@dataclass(frozen=True)
class Challenge:
    """A step the person answers before sign-in finishes.

    kind "code": type a code the provider sent (SMS, e-mail, authenticator).
    kind "captcha": type the characters in `image` (a data: URL).
    kind "paste": open `url`, sign in there, paste back what the page ends on.
    `state` stays on the server between the steps; it is never sent to the browser.
    """

    kind: Literal["code", "captcha", "paste"]
    prompt: str
    state: dict = field(default_factory=dict)
    image: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Line:
    """A home internet line on the account."""

    id: str  # stable: the landline number, service id, ...
    plan: str | None = None
    account: str | None = None  # the billing account; it can cover several lines
    meta: dict = field(default_factory=dict)  # provider-specific, kept for its own later calls


@dataclass(frozen=True)
class Bundle:
    """A plan or an extra bundle on a line. Sizes in bytes; None when unknown."""

    kind: Literal["plan", "extra"]
    name: str | None
    total_bytes: int | None  # some providers only say what is left of extra bundles
    remaining_bytes: int | None
    renews: dt.date | None = None
    label: str | None = None
    is_data: bool = True  # minutes and messages are listed but not added up

    def to_json(self) -> dict:
        data = asdict(self)
        data["renews"] = self.renews.isoformat() if self.renews else None
        return data


@dataclass(frozen=True)
class Usage:
    """What the provider says about a line right now."""

    bundles: list[Bundle]
    renews: dt.date | None = None  # when the plan renews next (the cycle's end)
    cycle_start: dt.date | None = None  # when the current cycle began, if the provider says

    @property
    def data(self) -> list[Bundle]:
        return [b for b in self.bundles if b.is_data and b.remaining_bytes is not None]

    @property
    def total(self) -> int | None:
        sizes = [b.total_bytes for b in self.data if b.total_bytes is not None]
        return sum(sizes) if sizes else None

    @property
    def remaining(self) -> int | None:
        data = self.data
        return sum(b.remaining_bytes or 0 for b in data) if data else None


class Provider:
    """Base class. Subclasses set the class attributes and implement the
    methods that raise NotImplementedError; the rest are optional."""

    id: ClassVar[str]
    name: ClassVar[str]  # as customers know it, e.g. "WE (Telecom Egypt)"
    country: ClassVar[str]  # ISO 3166 code, e.g. "EG"
    status: ClassVar[Status] = "beta"
    unit_base: ClassVar[int] = 1000  # 1024 when the provider counts 1 GB = 1024 MB
    currency: ClassVar[str | None] = None
    fields: ClassVar[tuple[Field, ...]] = ()
    notes: ClassVar[str] = ""  # one or two sentences under the sign-in form
    docs: ClassVar[str] = ""  # path in the repo, e.g. docs/providers/we_eg.md
    has_daily: ClassVar[bool] = False
    has_billing: ClassVar[bool] = False

    # --- signing in ---------------------------------------------------------------
    def account_name(self, values: dict[str, str]) -> str:
        """What identifies the account in Settings (a number, a user name)."""
        first = self.fields[0].name if self.fields else "account"
        return (values.get(first) or "").strip()

    def start_sign_in(self, values: dict[str, str]) -> dict | Challenge:
        raise NotImplementedError

    def answer(self, values: dict[str, str], challenge: Challenge, answer: str) -> dict | Challenge:
        raise ProviderError("This provider does not ask for a code")

    def refresh(self, credentials: dict) -> dict:
        """New credentials after a SessionExpired. Raise ProviderError(signed_out=True)
        when the person has to sign in again."""
        raise ProviderError("Signed out: sign in again from Settings", signed_out=True)

    # --- reading ------------------------------------------------------------------
    def lines(self, credentials: dict) -> list[Line]:
        raise NotImplementedError

    def usage(self, credentials: dict, line: Line) -> Usage:
        raise NotImplementedError

    def daily(self, credentials: dict, line: Line, start: dt.date, end: dt.date) -> dict[str, int]:
        """Bytes used per day, {"2026-10-10": bytes}, for providers that keep a history."""
        return {}

    def billing(self, credentials: dict, line: Line) -> dict | None:
        """{"open_amount": float|None, "unpaid": [{"due", "value"}], "bills": [{"from", "to", "issued", "due", "value"}]}"""
        return None

    @classmethod
    def match_wan_login(cls, login: str, line_ids: list[str]) -> str | None:
        """The line a router's internet (PPPoE) user name belongs to, if it says."""
        return None

    @classmethod
    def describe(cls) -> dict:
        return {
            "id": cls.id,
            "name": cls.name,
            "country": cls.country,
            "status": cls.status,
            "unit_base": cls.unit_base,
            "currency": cls.currency,
            "fields": [asdict(f) for f in cls.fields],
            "notes": cls.notes,
            "docs": cls.docs,
            "has_daily": cls.has_daily,
            "has_billing": cls.has_billing,
        }
