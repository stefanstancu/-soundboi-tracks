from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpotifyPlaylist:
    playlist_id: str
    name: str
    owner: str
    owner_id: str
    track_count: int
    accessible: bool


@dataclass(frozen=True)
class SpotifyTrack:
    track_id: str
    name: str
    artists: tuple[str, ...]
    album: str

    @property
    def query(self) -> str:
        artist = self.artists[0] if self.artists else ""
        return f"{artist} {self.name}".strip()

    @property
    def artist_label(self) -> str:
        return ", ".join(self.artists)
