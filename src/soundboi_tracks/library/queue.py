from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from soundboi_tracks.config import library_index_file
from soundboi_tracks.library.index import SearchOrigin, make_artist_title_key
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit
from soundboi_tracks.providers.spotify.models import SpotifyTrack


PENDING = "pending"
NEEDS_PURCHASE = "needs_purchase"
DOWNLOADING = "downloading"
COMPLETED = "completed"
FAILED = "failed"


@dataclass(frozen=True)
class QueueItem:
    queue_id: str
    hit: BandcampSearchHit
    status: str
    message: str = ""
    origin: SearchOrigin | None = None


class QueueStore:
    def __init__(self) -> None:
        self.path = library_index_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connect()
        self.reset_interrupted()

    def _connect(self) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_items (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_id TEXT,
                    result_type TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    url TEXT,
                    status TEXT NOT NULL,
                    message TEXT,
                    origin_source TEXT,
                    spotify_track_id TEXT,
                    spotify_title TEXT,
                    spotify_artists TEXT,
                    spotify_album TEXT,
                    spotify_playlist_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_provider ON queue_items(provider, provider_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_spotify ON queue_items(spotify_track_id)")
            conn.commit()

    def reset_interrupted(self) -> None:
        now = utc_now()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE queue_items SET status = ?, message = ?, updated_at = ? WHERE status = ?",
                (PENDING, "interrupted", now, DOWNLOADING),
            )
            conn.commit()

    def add(self, hit: BandcampSearchHit, origin: SearchOrigin | None = None) -> QueueItem:
        queue_id = queue_id_for_hit(hit)
        now = utc_now()
        provider_id = str(hit.item_id) if hit.item_id is not None else None
        origin_artists = json.dumps(list(origin.spotify_artists) if origin else [])
        with closing(sqlite3.connect(self.path)) as conn:
            existing = conn.execute("SELECT status FROM queue_items WHERE id = ?", (queue_id,)).fetchone()
            if existing:
                return self.get(queue_id)  # type: ignore[return-value]
            conn.execute(
                """
                INSERT INTO queue_items (
                    id, provider, provider_id, result_type, title, artist, album, url,
                    status, message, origin_source, spotify_track_id, spotify_title,
                    spotify_artists, spotify_album, spotify_playlist_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_id,
                    hit.source,
                    provider_id,
                    hit.result_type,
                    hit.name,
                    hit.artist,
                    hit.album,
                    hit.url,
                    PENDING,
                    "",
                    origin.source if origin else None,
                    origin.spotify_track_id if origin else None,
                    origin.spotify_title if origin else None,
                    origin_artists,
                    origin.spotify_album if origin else None,
                    origin.spotify_playlist_id if origin else None,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get(queue_id)  # type: ignore[return-value]

    def get(self, queue_id: str) -> QueueItem | None:
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (queue_id,)).fetchone()
        return item_from_row(row) if row else None

    def list(self) -> list[QueueItem]:
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT * FROM queue_items ORDER BY created_at").fetchall()
        return [item_from_row(row) for row in rows]

    def pending(self) -> list[QueueItem]:
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM queue_items WHERE status = ? ORDER BY created_at", (PENDING,)
            ).fetchall()
        return [item_from_row(row) for row in rows]

    def update_status(self, queue_id: str, status: str, message: str = "") -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE queue_items SET status = ?, message = ?, updated_at = ? WHERE id = ?",
                (status, message, utc_now(), queue_id),
            )
            conn.commit()

    def clear(self) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DELETE FROM queue_items")
            conn.commit()

    def status_for_hit(self, hit: BandcampSearchHit) -> str | None:
        queue_id = queue_id_for_hit(hit)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT status FROM queue_items WHERE id = ?", (queue_id,)).fetchone()
        return str(row[0]) if row else None

    def status_for_spotify_track(self, track: SpotifyTrack) -> str | None:
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT status FROM queue_items WHERE spotify_track_id = ? ORDER BY created_at DESC LIMIT 1",
                (track.track_id,),
            ).fetchone()
        return str(row[0]) if row else None


def queue_id_for_hit(hit: BandcampSearchHit) -> str:
    if hit.item_id is not None:
        return f"{hit.source}:{hit.item_id}"
    key = make_artist_title_key(hit.artist, hit.name) or "unknown"
    digest = hashlib.sha1(f"{hit.source}|{key}|{hit.url}".encode("utf-8")).hexdigest()[:16]
    return f"{hit.source}:fallback:{digest}"


def item_from_row(row: tuple[object, ...]) -> QueueItem:
    provider_id = str(row[2]) if row[2] is not None else None
    item_id = int(provider_id) if provider_id and provider_id.isdigit() else None
    hit = BandcampSearchHit(
        source=str(row[1]),
        rank=0,
        result_type=str(row[3] or ""),
        name=str(row[4] or ""),
        artist=str(row[5] or ""),
        album=str(row[6] or ""),
        url=str(row[7] or ""),
        item_id=item_id,
    )
    origin = None
    if row[10]:
        try:
            artists = tuple(json.loads(str(row[13] or "[]")))
        except json.JSONDecodeError:
            artists = ()
        origin = SearchOrigin(
            source=str(row[10]),
            spotify_track_id=str(row[11]) if row[11] else None,
            spotify_title=str(row[12]) if row[12] else None,
            spotify_artists=artists,
            spotify_album=str(row[14]) if row[14] else None,
            spotify_playlist_id=str(row[15]) if row[15] else None,
        )
    return QueueItem(
        queue_id=str(row[0]),
        hit=hit,
        status=str(row[8]),
        message=str(row[9] or ""),
        origin=origin,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
