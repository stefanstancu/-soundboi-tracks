from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urlsplit

from curl_cffi import requests

from soundboi_tracks.config import (
    beatport_token_file,
    library_incoming_dir,
    library_staging_dir,
    orpheusdl_dir,
    write_private_text,
)
from soundboi_tracks.library.index import is_audio_file, move_files_to_library
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit


BEATPORT_API_URL = "https://api.beatport.com/v4/"
BEATPORT_CLIENT_ID = "Zy2K9Wvy6DkUds7g8s1GNMHfk17E5Ch2BWHlyaGY"
BEATPORT_REDIRECT_URI = "seratodjlite://beatport"


class BeatportSearchError(RuntimeError):
    pass


class BeatportDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class BeatportCredentials:
    username: str
    password: str


@dataclass(frozen=True)
class BeatportDownloadResult:
    track_id: int
    output_dir: Path
    files: tuple[Path, ...] = ()


class BeatportClient:
    def __init__(self) -> None:
        self.session = requests.Session(impersonate="chrome")
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires: datetime | None = None
        self._load_tokens()

    def search_tracks(self, query: str, limit: int = 15) -> list[BandcampSearchHit]:
        self.ensure_authenticated()
        data = self._get("catalog/search", params={"q": query})
        tracks = data.get("tracks") or []
        if not isinstance(tracks, list):
            return []
        hits = []
        for rank, track in enumerate(tracks[:limit], start=1):
            if not isinstance(track, dict):
                continue
            track_artists = track.get("artists", [])
            if not isinstance(track_artists, list):
                track_artists = []
            artists = ", ".join(
                str(artist.get("name", "")) for artist in track_artists if isinstance(artist, dict)
            )
            name = str(track.get("name") or "")
            mix_name = track.get("mix_name")
            if mix_name:
                name = f"{name} ({mix_name})"
            release = track.get("release") or {}
            if not isinstance(release, dict):
                release = {}
            track_id = track.get("id")
            hits.append(
                BandcampSearchHit(
                    source="beatport",
                    rank=rank,
                    result_type="track",
                    name=name,
                    artist=artists,
                    album=str(release.get("name") or ""),
                    url=beatport_track_url(track),
                    item_id=int(track_id) if track_id is not None else None,
                )
            )
        return hits

    def ensure_authenticated(self) -> None:
        if self.access_token and self.refresh_token and self.expires and datetime.now() < self.expires:
            return
        if self.refresh_token:
            try:
                self._refresh()
                return
            except Exception:
                self.access_token = None
                self.refresh_token = None
                self.expires = None
        credentials = load_beatport_credentials()
        self._login(credentials)

    def _headers(self, use_access_token: bool = False) -> dict[str, str]:
        headers = {"user-agent": "libbeatport/v2.8.2"}
        if use_access_token and self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        return headers

    def _get(self, endpoint: str, params: dict[str, str] | None = None) -> dict[str, object]:
        response = self.session.get(
            f"{BEATPORT_API_URL}{endpoint}",
            params=params or {},
            headers=self._headers(use_access_token=True),
            timeout=30,
        )
        if response.status_code == 401:
            self._refresh()
            response = self.session.get(
                f"{BEATPORT_API_URL}{endpoint}",
                params=params or {},
                headers=self._headers(use_access_token=True),
                timeout=30,
            )
        if response.status_code not in {200, 201, 202}:
            raise BeatportSearchError(f"Beatport API returned HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise BeatportSearchError("Beatport API returned unexpected JSON")
        return data

    def _login(self, credentials: BeatportCredentials) -> None:
        browser_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        response = self.session.get(
            f"{BEATPORT_API_URL}auth/o/authorize/",
            params={
                "client_id": BEATPORT_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": BEATPORT_REDIRECT_URI,
            },
            headers=browser_headers,
            allow_redirects=False,
            timeout=30,
        )
        if response.status_code != 302:
            raise BeatportSearchError("Beatport authorization did not return a login redirect")
        parsed_url = urlsplit(str(response.url))
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        referer = base_url + response.headers["location"]

        response = self.session.post(
            f"{BEATPORT_API_URL}auth/login/",
            json={"username": credentials.username, "password": credentials.password},
            headers={**browser_headers, "Referer": referer},
            timeout=30,
        )
        if response.status_code != 200:
            raise BeatportSearchError("Beatport login failed")

        response = self.session.get(
            f"{BEATPORT_API_URL}auth/o/authorize/",
            params={
                "client_id": BEATPORT_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": BEATPORT_REDIRECT_URI,
            },
            headers=browser_headers,
            allow_redirects=False,
            timeout=30,
        )
        if response.status_code != 302:
            raise BeatportSearchError("Beatport authorization did not return an auth code")
        code = parse_qs(urlsplit(response.headers["location"]).query).get("code", [None])[0]
        if not code:
            raise BeatportSearchError("Beatport authorization response did not include an auth code")

        response = self.session.post(
            f"{BEATPORT_API_URL}auth/o/token/",
            data={
                "client_id": BEATPORT_CLIENT_ID,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": BEATPORT_REDIRECT_URI,
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise BeatportSearchError("Beatport token exchange failed")
        self._set_tokens(response.json())

    def _refresh(self) -> None:
        if not self.refresh_token:
            raise BeatportSearchError("Beatport refresh token is missing")
        response = self.session.post(
            f"{BEATPORT_API_URL}auth/o/token/",
            data={
                "client_id": BEATPORT_CLIENT_ID,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise BeatportSearchError("Beatport token refresh failed")
        self._set_tokens(response.json())

    def _set_tokens(self, data: dict[str, object]) -> None:
        self.access_token = str(data["access_token"])
        self.refresh_token = str(data["refresh_token"])
        self.expires = datetime.now() + timedelta(seconds=int(str(data["expires_in"])))
        self._save_tokens()

    def _load_tokens(self) -> None:
        path = beatport_token_file()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            expires = data.get("expires")
            self.expires = datetime.fromisoformat(expires) if expires else None
        except Exception:
            self.access_token = None
            self.refresh_token = None
            self.expires = None

    def _save_tokens(self) -> None:
        write_private_text(
            beatport_token_file(),
            json.dumps(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires": self.expires.isoformat() if self.expires else None,
                },
                indent=2,
            ),
        )


def load_beatport_credentials() -> BeatportCredentials:
    settings_path = orpheusdl_dir() / "config" / "settings.json"
    if not settings_path.exists():
        raise BeatportSearchError(f"OrpheusDL settings not found at {settings_path}")
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    module_settings = data.get("modules", {}).get("beatport", {})
    username = str(module_settings.get("username") or "")
    password = str(module_settings.get("password") or "")
    if not username or not password:
        raise BeatportSearchError("Beatport credentials are missing from OrpheusDL settings")
    return BeatportCredentials(username=username, password=password)


def beatport_track_url(track: dict[str, object]) -> str:
    track_id = track.get("id")
    slug = track.get("slug") or str(track.get("name") or "").lower().replace(" ", "-")
    slug = quote(str(slug), safe="")
    if not track_id:
        return "https://www.beatport.com/"
    return f"https://www.beatport.com/track/{slug}/{track_id}"


def search_beatport(query: str, limit: int = 15) -> list[BandcampSearchHit]:
    query = query.strip()
    if not query:
        return []
    return BeatportClient().search_tracks(query, limit=limit)


def download_beatport_track(
    track_id: int,
    output_dir: Path | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> BeatportDownloadResult:
    final_output_dir = output_dir or library_incoming_dir()
    staging_dir = library_staging_dir() / f"beatport-{track_id}-{int(datetime.now().timestamp())}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "orpheus.py",
        "-o",
        str(staging_dir),
        "download",
        "beatport",
        "track",
        str(track_id),
    ]
    process = subprocess.Popen(
        command,
        cwd=orpheusdl_dir(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines: list[str] = []
    try:
        if process.stdout:
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                output_lines.append(line)
                if log_callback:
                    log_callback(line)
        return_code = process.wait(timeout=600)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise BeatportDownloadError("Beatport download timed out") from exc
    if return_code != 0:
        message = "\n".join(output_lines[-20:]) or "Beatport download failed"
        raise BeatportDownloadError(message[-1000:])
    new_files = [path for path in staging_dir.rglob("*") if is_audio_file(path)]
    final_output_dir.mkdir(parents=True, exist_ok=True)
    moved_files = move_files_to_library(new_files, final_output_dir)
    try:
        for leftover in sorted(staging_dir.rglob("*"), reverse=True):
            if leftover.is_file():
                leftover.unlink()
            elif leftover.is_dir():
                leftover.rmdir()
        staging_dir.rmdir()
    except OSError:
        pass
    return BeatportDownloadResult(track_id=track_id, output_dir=final_output_dir, files=tuple(moved_files))
