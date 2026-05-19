from __future__ import annotations

import json
import time
from dataclasses import dataclass
from html import unescape
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Self

from bs4 import BeautifulSoup
from curl_cffi import requests

from soundboi_tracks.config import (
    bandcamp_browser_dir,
    bandcamp_cookie_file,
    read_text_if_exists,
    write_private_text,
)


BANDCAMP_HOME = "https://bandcamp.com/"
BANDCAMP_LOGIN = "https://bandcamp.com/login"
PRIMARY_AUTH_COOKIE = "identity"
SUPPORTING_AUTH_COOKIES = {"client_id", "js_logged_in"}


class BandcampAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class BandcampAuthStatus:
    authenticated: bool
    fan_id: int | None = None
    username: str | None = None
    cookie_path: Path | None = None
    message: str = ""


@dataclass(frozen=True)
class CapturedCookies:
    header: str
    names: tuple[str, ...]


class BandcampBrowserLogin:
    def __init__(self) -> None:
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._context is not None

    def start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BandcampAuthError("Playwright is not installed. Run pip install -e .") from exc

        browser_path = bandcamp_browser_dir()
        browser_path.mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(
                str(browser_path),
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            self._playwright = playwright
            self._context = context
            self._page = page
            page.goto(BANDCAMP_LOGIN, wait_until="domcontentloaded")
        except Exception:
            self._playwright = playwright
            self.close()
            raise

    def capture(self) -> CapturedCookies:
        if self._context is None:
            raise BandcampAuthError("Bandcamp login browser is not open")
        try:
            cookies = self._context.cookies()
        except Exception as exc:
            self.close()
            raise BandcampAuthError("Bandcamp login browser was closed") from exc
        names = tuple(sorted({cookie.get("name", "") for cookie in cookies if cookie.get("name")}))
        return CapturedCookies(header=cookies_to_header(cookies), names=names)

    def finish(self) -> BandcampAuthStatus:
        captured = self.capture()
        if not captured.header:
            return BandcampAuthStatus(
                False,
                cookie_path=bandcamp_cookie_file(),
                message="No Bandcamp cookies found in login browser",
            )
        if not has_required_auth_cookies(captured.header):
            return BandcampAuthStatus(
                False,
                cookie_path=bandcamp_cookie_file(),
                message=(
                    f"{missing_auth_cookie_description(captured.header)}. Saw: "
                    + (", ".join(captured.names) if captured.names else "no cookies")
                ),
            )

        save_cookie_header(captured.header)
        status = verify_cookie_header(captured.header)
        if status.authenticated:
            self.close()
        return status

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


def open_bandcamp_page_and_wait_for_close(url: str, timeout_seconds: int = 900) -> BandcampAuthStatus:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BandcampAuthError("Playwright is not installed. Run pip install -e .") from exc

    deadline = time.monotonic() + timeout_seconds
    browser_path = bandcamp_browser_dir()
    browser_path.mkdir(parents=True, exist_ok=True)
    last_cookie_header = ""

    playwright = sync_playwright().start()
    context = None
    try:
        context = playwright.chromium.launch_persistent_context(
            str(browser_path),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        while time.monotonic() < deadline:
            if page.is_closed():
                break
            try:
                cookie_header = cookies_to_header(context.cookies())
                if cookie_header:
                    last_cookie_header = cookie_header
                page.wait_for_timeout(1000)
            except PlaywrightError as exc:
                if _is_playwright_closed_error(exc):
                    break
                raise
            except Exception:
                break

        try:
            cookie_header = cookies_to_header(context.cookies())
            if cookie_header:
                last_cookie_header = cookie_header
        except Exception:
            pass
    finally:
        if last_cookie_header and has_required_auth_cookies(last_cookie_header):
            save_cookie_header(last_cookie_header)
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        playwright.stop()

    return verify_cookie_header(last_cookie_header or None)


def _is_playwright_closed_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return "closed" in message and (
        "target page" in message or "target context" in message or "target browser" in message
    )


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    parts = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain", "")
        if not name or value is None:
            continue
        if "bandcamp.com" not in domain:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def has_required_auth_cookies(cookie_header: str) -> bool:
    return not missing_auth_cookie_description(cookie_header)


def missing_auth_cookie_description(cookie_header: str) -> str:
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_header)
    except Exception:
        return "Could not parse cookies"
    names = set(parsed.keys())
    if PRIMARY_AUTH_COOKIE not in names:
        return "Missing identity cookie"
    if not SUPPORTING_AUTH_COOKIES.intersection(names):
        return "Missing supporting login cookie: client_id or js_logged_in"
    return ""


def save_cookie_header(cookie_header: str) -> Path:
    path = bandcamp_cookie_file()
    write_private_text(path, cookie_header)
    return path


def load_cookie_header() -> str | None:
    return read_text_if_exists(bandcamp_cookie_file())


def _session_from_cookie_header(cookie_header: str) -> requests.Session:
    session = requests.Session(impersonate="chrome")
    parsed = SimpleCookie()
    parsed.load(cookie_header)
    for name, morsel in parsed.items():
        session.cookies.set(name, morsel.value, domain=".bandcamp.com")
    return session


def authenticated_session(cookie_header: str | None = None) -> requests.Session:
    cookie_header = cookie_header or load_cookie_header()
    if not cookie_header:
        raise BandcampAuthError("No Bandcamp cookies saved")
    status = verify_cookie_header(cookie_header)
    if not status.authenticated:
        raise BandcampAuthError(status.message or "Bandcamp cookies are not authenticated")
    return _session_from_cookie_header(cookie_header)


def _extract_homepage_blob(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("div", id="HomepageApp")
    if not tag:
        raise BandcampAuthError("Bandcamp homepage did not expose HomepageApp data")
    blob = tag.attrs.get("data-blob")
    if not blob:
        raise BandcampAuthError("Bandcamp HomepageApp data was empty")
    try:
        return json.loads(unescape(blob))
    except json.JSONDecodeError as exc:
        raise BandcampAuthError("Bandcamp HomepageApp data was not valid JSON") from exc


def verify_cookie_header(cookie_header: str | None = None) -> BandcampAuthStatus:
    cookie_header = cookie_header or load_cookie_header()
    if not cookie_header:
        return BandcampAuthStatus(False, cookie_path=bandcamp_cookie_file(), message="No cookies saved")
    if not has_required_auth_cookies(cookie_header):
        return BandcampAuthStatus(
            False,
            cookie_path=bandcamp_cookie_file(),
            message=missing_auth_cookie_description(cookie_header),
        )

    session = _session_from_cookie_header(cookie_header)
    response = session.get(BANDCAMP_HOME)
    if response.status_code != 200:
        return BandcampAuthStatus(
            False,
            cookie_path=bandcamp_cookie_file(),
            message=f"Bandcamp returned HTTP {response.status_code}",
        )

    try:
        page_data = _extract_homepage_blob(response.text)
        identity = page_data["pageContext"]["identity"]
    except (BandcampAuthError, KeyError, TypeError) as exc:
        return BandcampAuthStatus(
            False,
            cookie_path=bandcamp_cookie_file(),
            message=f"Could not verify identity: {exc}",
        )

    fan_id = identity.get("fanId")
    username = identity.get("username") or identity.get("name")
    if not fan_id:
        return BandcampAuthStatus(
            False,
            cookie_path=bandcamp_cookie_file(),
            message="Bandcamp identity did not include a fan id",
        )

    return BandcampAuthStatus(
        True,
        fan_id=int(fan_id),
        username=username,
        cookie_path=bandcamp_cookie_file(),
        message="Authenticated",
    )


def login_with_browser(timeout_seconds: int = 300) -> BandcampAuthStatus:
    deadline = time.monotonic() + timeout_seconds
    with BandcampBrowserLogin() as login:
        last_cookie_header = ""
        while time.monotonic() < deadline:
            captured = login.capture()
            last_cookie_header = captured.header or last_cookie_header
            if has_required_auth_cookies(captured.header):
                save_cookie_header(captured.header)
                status = verify_cookie_header(captured.header)
                if status.authenticated:
                    return status
            time.sleep(1)

    if last_cookie_header:
        save_cookie_header(last_cookie_header)
    raise BandcampAuthError("Timed out waiting for Bandcamp login")
