"""A small HTTP client for provider and router plugins (standard library only).

It keeps cookies, sends JSON or form bodies, can stop at redirects (sign-in
flows read the Location header), and turns network failures into
ProviderError. Tests swap the network for a `transport`: a function that takes
a `Request` and returns a `Response`.
"""

from __future__ import annotations

import http.cookiejar
import json as jsonlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import ProviderError

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Tattle (+https://github.com/khaledsharara/tattle)"


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # names in lower case
    text: str
    url: str

    def json(self) -> Any:
        try:
            return jsonlib.loads(self.text) if self.text else None
        except ValueError as exc:
            raise ProviderError("The provider sent an answer Tattle could not read", status=self.status) from exc

    @property
    def location(self) -> str | None:
        return self.headers.get("location")


Transport = Callable[[Request], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        return None


class Http:
    def __init__(
        self,
        base_url: str = "",
        *,
        timeout: float = 20.0,
        user_agent: str = USER_AGENT,
        transport: Transport | None = None,
        verify_tls: bool = True,
        who: str = "the provider",
        error: type[Exception] = ProviderError,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.transport = transport
        self.who = who  # for error messages: "Could not reach WE"
        self.error = error  # what network failures raise (router adapters use RouterError)
        self.jar = http.cookiejar.CookieJar()
        context = None if verify_tls else ssl._create_unverified_context()  # routers with self-signed certificates
        handlers: list = [urllib.request.HTTPCookieProcessor(self.jar)]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._follow = urllib.request.build_opener(*handlers)
        self._stay = urllib.request.build_opener(*handlers, _NoRedirect())

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        full = path if path.startswith(("http://", "https://")) else self.base_url + path
        if params:
            full += ("&" if "?" in full else "?") + urllib.parse.urlencode(params)
        return full

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        form: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> Response:
        sent = {"User-Agent": self.user_agent, "Accept": "application/json, text/plain, */*"}
        if json is not None:
            body = jsonlib.dumps(json).encode()
            sent["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent.update(headers or {})
        req = Request(method.upper(), self.url(path, params), sent, body)
        if self.transport is not None:
            return self.transport(req)
        return self._send(req, follow_redirects)

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def _send(self, req: Request, follow_redirects: bool) -> Response:
        raw = urllib.request.Request(req.url, data=req.body, method=req.method)
        for name, value in req.headers.items():
            raw.add_header(name, value)
        opener = self._follow if follow_redirects else self._stay
        try:
            with opener.open(raw, timeout=self.timeout) as answer:
                return Response(answer.status, _lower(answer.headers.items()), _decode(answer.read()), answer.geturl())
        except urllib.error.HTTPError as exc:
            headers = _lower(exc.headers.items() if exc.headers else [])
            return Response(exc.code, headers, _decode(exc.read() if exc.fp else b""), req.url)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise self.error(f"Could not reach {self.who}: {reason}") from exc

    # Cookies survive restarts by living in the credentials dict.
    def export_cookies(self) -> list[dict]:
        return [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure, "expires": c.expires}
            for c in self.jar
        ]

    def import_cookies(self, cookies: list[dict] | None) -> None:
        for c in cookies or []:
            self.jar.set_cookie(
                http.cookiejar.Cookie(
                    0, c["name"], c["value"], None, False, c["domain"], bool(c["domain"]), c["domain"].startswith("."),
                    c.get("path") or "/", True, bool(c.get("secure")), c.get("expires"), False, None, None, {},
                )
            )


def _lower(items) -> dict[str, str]:
    return {name.lower(): value for name, value in items}


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")
