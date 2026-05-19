from __future__ import annotations

from typing import Any

from curl_cffi import requests

from soundboi_tracks.providers.spotify.auth import SpotifyAuthError, get_access_token
from soundboi_tracks.providers.spotify.models import SpotifyPlaylist, SpotifyTrack


SPOTIFY_API_URL = "https://api.spotify.com/v1"


class SpotifyClientError(RuntimeError):
    pass


class SpotifyClient:
    def __init__(self) -> None:
        self.access_token = get_access_token()
        self.user_id = self._current_user_id()

    def list_playlists(self, limit: int = 50) -> list[SpotifyPlaylist]:
        playlists: list[SpotifyPlaylist] = []
        url = f"{SPOTIFY_API_URL}/me/playlists"
        params: dict[str, int] | None = {"limit": limit}
        while url:
            data = self._get(url, params=params)
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    playlists.append(_playlist_from_item(item, current_user_id=self.user_id))
            url = data.get("next")
            params = None
        return sorted(playlists, key=lambda playlist: (not playlist.accessible, playlist.name.casefold()))

    def list_playlist_tracks(self, playlist_id: str, limit: int = 100) -> list[SpotifyTrack]:
        tracks: list[SpotifyTrack] = []
        url = f"{SPOTIFY_API_URL}/playlists/{playlist_id}/items"
        params: dict[str, str | int] | None = {
            "limit": min(limit, 50),
            "fields": "next,items(item(id,name,artists(name),album(name),type))",
        }
        while url:
            data = self._get(url, params=params)
            for item in data.get("items") or []:
                if isinstance(item, dict) and isinstance(item.get("item"), dict):
                    track = _track_from_item(item["item"])
                    if track:
                        tracks.append(track)
            url = data.get("next")
            params = None
        return tracks

    def _current_user_id(self) -> str:
        data = self._get(f"{SPOTIFY_API_URL}/me")
        return str(data.get("id") or "")

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=30,
        )
        if response.status_code == 401:
            raise SpotifyAuthError("Spotify token is invalid or expired; log in again")
        if response.status_code != 200:
            detail = response.text.strip().replace("\n", " ")[:500]
            raise SpotifyClientError(f"Spotify API returned HTTP {response.status_code}: {detail}")
        data = response.json()
        if not isinstance(data, dict):
            raise SpotifyClientError("Spotify API returned unexpected JSON")
        return data


def _playlist_from_item(item: dict[str, Any], current_user_id: str) -> SpotifyPlaylist:
    owner = item.get("owner") or {}
    tracks = item.get("items") or item.get("tracks") or {}
    owner_id = str(owner.get("id") or "")
    collaborative = bool(item.get("collaborative"))
    return SpotifyPlaylist(
        playlist_id=str(item.get("id") or ""),
        name=str(item.get("name") or ""),
        owner=str(owner.get("display_name") or owner.get("id") or ""),
        owner_id=owner_id,
        track_count=int(tracks.get("total") or 0),
        accessible=owner_id == current_user_id or collaborative,
    )


def _track_from_item(item: dict[str, Any]) -> SpotifyTrack | None:
    if item.get("type") not in {None, "track"}:
        return None
    track_id = item.get("id")
    name = item.get("name")
    if not track_id or not name:
        return None
    artists = tuple(
        str(artist.get("name") or "")
        for artist in item.get("artists", [])
        if isinstance(artist, dict) and artist.get("name")
    )
    album = item.get("album") or {}
    return SpotifyTrack(
        track_id=str(track_id),
        name=str(name),
        artists=artists,
        album=str(album.get("name") or ""),
    )
