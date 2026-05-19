from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from curl_cffi import requests

from soundboi_tracks.config import (
    read_text_if_exists,
    spotify_config_file,
    spotify_token_file,
    write_private_text,
)


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8765/callback"
SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"


class SpotifyAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotifyToken:
    access_token: str
    refresh_token: str | None
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 60


def load_spotify_client_id() -> str:
    env_value = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if env_value:
        return env_value
    raw = read_text_if_exists(spotify_config_file())
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SpotifyAuthError(f"Invalid Spotify config JSON at {spotify_config_file()}") from exc
        client_id = str(data.get("client_id") or "").strip()
        if client_id:
            return client_id
    raise SpotifyAuthError(
        "Spotify client id is missing. Set SPOTIFY_CLIENT_ID or write "
        f'{spotify_config_file()} with {{"client_id": "..."}}.'
    )


def load_token() -> SpotifyToken | None:
    raw = read_text_if_exists(spotify_token_file())
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return SpotifyToken(
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
        )
    except Exception:
        return None


def save_token(token: SpotifyToken) -> Path:
    path = spotify_token_file()
    write_private_text(
        path,
        json.dumps(
            {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
            },
            indent=2,
        ),
    )
    return path


def get_access_token() -> str:
    token = load_token()
    if not token:
        raise SpotifyAuthError("Spotify is not authenticated")
    if token.expired:
        token = refresh_token(token)
    return token.access_token


def refresh_token(token: SpotifyToken) -> SpotifyToken:
    if not token.refresh_token:
        raise SpotifyAuthError("Spotify refresh token is missing; log in again")
    client_id = load_spotify_client_id()
    response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise SpotifyAuthError(f"Spotify token refresh returned HTTP {response.status_code}")
    data = response.json()
    refreshed = SpotifyToken(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token") or token.refresh_token),
        expires_at=time.time() + int(data["expires_in"]),
    )
    save_token(refreshed)
    return refreshed


def login(timeout_seconds: int = 180) -> Path:
    client_id = load_spotify_client_id()
    verifier = _code_verifier()
    challenge = _code_challenge(verifier)
    state = secrets.token_urlsafe(24)
    callback = _wait_for_callback(
        _auth_url(client_id=client_id, challenge=challenge, state=state),
        timeout_seconds=timeout_seconds,
    )
    if callback.get("state") != state:
        raise SpotifyAuthError("Spotify auth state did not match")
    code = callback.get("code")
    if not code:
        raise SpotifyAuthError(callback.get("error") or "Spotify auth did not return a code")
    token = _exchange_code(client_id=client_id, code=code, verifier=verifier)
    return save_token(token)


def _auth_url(client_id: str, challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }
    return f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _wait_for_callback(auth_url: str, timeout_seconds: int) -> dict[str, str]:
    parsed_redirect = urllib.parse.urlsplit(SPOTIFY_REDIRECT_URI)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal result
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != parsed_redirect.path:
                self.send_response(404)
                self.end_headers()
                return
            values = urllib.parse.parse_qs(parsed.query)
            result = {key: value[0] for key, value in values.items() if value}
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Spotify login complete.</h1>You can close this tab.</body></html>")

        def log_message(self, format: str, *_args: Any) -> None:
            _ = format
            return

    server = HTTPServer((parsed_redirect.hostname or "127.0.0.1", parsed_redirect.port or 8765), Handler)
    server.timeout = timeout_seconds
    webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()
    if not result:
        raise SpotifyAuthError("Timed out waiting for Spotify login callback")
    return result


def _exchange_code(client_id: str, code: str, verifier: str) -> SpotifyToken:
    response = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise SpotifyAuthError(f"Spotify token exchange returned HTTP {response.status_code}")
    data = response.json()
    return SpotifyToken(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token") or ""),
        expires_at=time.time() + int(data["expires_in"]),
    )


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
