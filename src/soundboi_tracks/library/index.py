from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from soundboi_tracks.config import library_incoming_dir, library_index_file
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit
from soundboi_tracks.providers.spotify.models import SpotifyTrack


AUDIO_SUFFIXES = {".aac", ".aif", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".wav"}


@dataclass(frozen=True)
class SearchOrigin:
    source: str
    spotify_track_id: str | None = None
    spotify_title: str | None = None
    spotify_artists: tuple[str, ...] = ()
    spotify_album: str | None = None
    spotify_playlist_id: str | None = None

    @property
    def label(self) -> str:
        artist = self.spotify_artists[0] if self.spotify_artists else ""
        title = self.spotify_title or ""
        return f"{artist} - {title}" if artist else title


@dataclass(frozen=True)
class LocalMatch:
    status: str
    path: Path | None = None

    @property
    def label(self) -> str:
        return {"exact": "✓", "likely": "~"}.get(self.status, "")


class LibraryIndex:
    def __init__(self) -> None:
        self.path = library_index_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._provider_keys: dict[str, Path] = {}
        self._spotify_keys: dict[str, Path] = {}
        self._artist_title_keys: dict[str, Path] = {}
        self._connect()
        self.refresh_memory()

    def _connect(self) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    provider TEXT,
                    provider_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    url TEXT,
                    spotify_track_id TEXT,
                    spotify_title TEXT,
                    spotify_artists TEXT,
                    spotify_album TEXT,
                    spotify_playlist_id TEXT,
                    provider_key TEXT,
                    artist_title_key TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_provider_key ON tracks(provider_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_spotify ON tracks(spotify_track_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist_title_key)")
            conn.commit()

    def refresh_memory(self) -> None:
        provider_keys: dict[str, Path] = {}
        spotify_keys: dict[str, Path] = {}
        artist_title_keys: dict[str, Path] = {}
        with closing(sqlite3.connect(self.path)) as conn:
            for row in conn.execute(
                "SELECT path, provider_key, spotify_track_id, artist_title_key FROM tracks"
            ):
                path = Path(row[0])
                if row[1]:
                    provider_keys[str(row[1])] = path
                if row[2]:
                    spotify_keys[str(row[2])] = path
                if row[3]:
                    artist_title_keys.setdefault(str(row[3]), path)
        self._provider_keys = provider_keys
        self._spotify_keys = spotify_keys
        self._artist_title_keys = artist_title_keys

    def scan(self, roots: Iterable[Path] | None = None) -> None:
        roots = tuple(roots or (library_incoming_dir(),))
        with closing(sqlite3.connect(self.path)) as conn:
            for root in roots:
                if not root.exists():
                    continue
                for file_path in root.rglob("*"):
                    if not is_audio_file(file_path):
                        continue
                    stat = file_path.stat()
                    row = conn.execute(
                        "SELECT size, mtime_ns FROM tracks WHERE path = ?", (str(file_path),)
                    ).fetchone()
                    if row and row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
                        continue
                    self._upsert(conn, file_path, stat.st_size, stat.st_mtime_ns, {})

            existing = [row[0] for row in conn.execute("SELECT path FROM tracks")]
            for path in existing:
                if not Path(path).exists():
                    conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
            conn.commit()
        self.refresh_memory()

    def record_download(
        self,
        file_path: Path,
        hit: BandcampSearchHit,
        origin: SearchOrigin | None = None,
    ) -> None:
        metadata = metadata_from_hit(hit, origin)
        stat = file_path.stat()
        with closing(sqlite3.connect(self.path)) as conn:
            self._upsert(conn, file_path, stat.st_size, stat.st_mtime_ns, metadata)
            conn.commit()
        self.refresh_memory()

    def match_hit(self, hit: BandcampSearchHit) -> LocalMatch:
        provider_key = make_provider_key(hit.source, hit.item_id)
        if provider_key and provider_key in self._provider_keys:
            return LocalMatch("exact", self._provider_keys[provider_key])
        artist_title_key = make_artist_title_key(hit.artist, hit.name)
        if artist_title_key and artist_title_key in self._artist_title_keys:
            return LocalMatch("likely", self._artist_title_keys[artist_title_key])
        return LocalMatch("none")

    def match_spotify_track(self, track: SpotifyTrack) -> LocalMatch:
        if track.track_id in self._spotify_keys:
            return LocalMatch("exact", self._spotify_keys[track.track_id])
        artist = track.artists[0] if track.artists else ""
        artist_title_key = make_artist_title_key(artist, track.name)
        if artist_title_key and artist_title_key in self._artist_title_keys:
            return LocalMatch("likely", self._artist_title_keys[artist_title_key])
        return LocalMatch("none")

    def _upsert(
        self,
        conn: sqlite3.Connection,
        file_path: Path,
        size: int,
        mtime_ns: int,
        metadata: dict[str, object],
    ) -> None:
        provider = str(metadata.get("provider") or "") or None
        provider_id = str(metadata.get("provider_id") or "") or None
        title = str(metadata.get("title") or "") or None
        artist = str(metadata.get("artist") or "") or None
        album = str(metadata.get("album") or "") or None
        url = str(metadata.get("url") or "") or None
        origin = metadata.get("origin") if isinstance(metadata.get("origin"), dict) else {}
        spotify_track_id = str(origin.get("spotify_track_id") or "") or None
        spotify_title = str(origin.get("spotify_title") or "") or None
        spotify_artists = json.dumps(origin.get("spotify_artists") or [])
        spotify_album = str(origin.get("spotify_album") or "") or None
        spotify_playlist_id = str(origin.get("spotify_playlist_id") or "") or None
        conn.execute(
            """
            INSERT OR REPLACE INTO tracks (
                path, size, mtime_ns, provider, provider_id, title, artist, album, url,
                spotify_track_id, spotify_title, spotify_artists, spotify_album, spotify_playlist_id,
                provider_key, artist_title_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(file_path),
                size,
                mtime_ns,
                provider,
                provider_id,
                title,
                artist,
                album,
                url,
                spotify_track_id,
                spotify_title,
                spotify_artists,
                spotify_album,
                spotify_playlist_id,
                make_provider_key(provider, provider_id),
                make_artist_title_key(artist, title),
            ),
        )


def move_files_to_library(files: Iterable[Path], destination: Path | None = None) -> list[Path]:
    destination = destination or library_incoming_dir()
    destination.mkdir(parents=True, exist_ok=True)
    moved = []
    for file_path in files:
        if not is_audio_file(file_path):
            continue
        target = unique_path(destination / file_path.name)
        if file_path.resolve() == target.resolve():
            moved.append(file_path)
            continue
        shutil.move(str(file_path), target)
        moved.append(target)
    return moved


def metadata_from_hit(hit: BandcampSearchHit, origin: SearchOrigin | None = None) -> dict[str, object]:
    data: dict[str, object] = {
        "provider": hit.source,
        "provider_id": str(hit.item_id) if hit.item_id is not None else None,
        "title": hit.name,
        "artist": hit.artist,
        "album": hit.album,
        "url": hit.url,
    }
    if origin:
        data["origin"] = {
            "source": origin.source,
            "spotify_track_id": origin.spotify_track_id,
            "spotify_title": origin.spotify_title,
            "spotify_artists": list(origin.spotify_artists),
            "spotify_album": origin.spotify_album,
            "spotify_playlist_id": origin.spotify_playlist_id,
        }
    return data


def is_audio_file(file_path: Path) -> bool:
    return file_path.is_file() and file_path.suffix.casefold() in AUDIO_SUFFIXES


def normalize_text(value: str | None) -> str:
    value = (value or "").casefold()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def make_artist_title_key(artist: str | None, title: str | None) -> str | None:
    artist_key = normalize_text(artist)
    title_key = normalize_text(title)
    if not artist_key or not title_key:
        return None
    return f"{artist_key}::{title_key}"


def make_provider_key(provider: str | None, provider_id: object | None) -> str | None:
    if not provider or provider_id in (None, ""):
        return None
    return f"{provider}:{provider_id}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique path for {path}")
