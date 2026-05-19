from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from soundboi_tracks.providers.bandcamp.auth import (
    BANDCAMP_HOME,
    BandcampAuthError,
    authenticated_session,
    verify_cookie_header,
)
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit


COLLECTION_ITEMS_URL = "https://bandcamp.com/api/fancollection/1/collection_items"


class BandcampCollectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BandcampPurchase:
    item_id: int
    item_type: str
    title: str
    artist: str
    album: str
    sale_item_type: str
    sale_item_id: int | None
    redownload_url: str | None
    track_titles: tuple[str, ...] = ()

    @property
    def redownload_available(self) -> bool:
        return bool(self.redownload_url)


@dataclass(frozen=True)
class OwnershipMatch:
    owned: bool
    match_type: str | None = None
    purchase_item_id: int | None = None
    redownload_available: bool = False


class BandcampCollection:
    def __init__(self, purchases: list[BandcampPurchase]) -> None:
        self.purchases = purchases
        self._by_type_and_id: dict[tuple[str, int], BandcampPurchase] = {}
        self._by_artist_title: dict[tuple[str, str], BandcampPurchase] = {}
        self._album_tracks_by_artist_title: dict[tuple[str, str], BandcampPurchase] = {}
        self._index()

    def _index(self) -> None:
        for purchase in self.purchases:
            self._by_type_and_id[(purchase.item_type, purchase.item_id)] = purchase
            artist_key = normalize_text(purchase.artist)
            title_key = normalize_text(purchase.title)
            if artist_key and title_key:
                self._by_artist_title[(artist_key, title_key)] = purchase
            if purchase.item_type == "album":
                for track_title in purchase.track_titles:
                    track_key = normalize_text(track_title)
                    if artist_key and track_key:
                        self._album_tracks_by_artist_title[(artist_key, track_key)] = purchase

    def find_purchase_for_hit(self, hit: BandcampSearchHit) -> tuple[BandcampPurchase | None, str | None]:
        if hit.item_id is not None:
            exact = self._by_type_and_id.get((hit.result_type, hit.item_id))
            if exact:
                return exact, "exact item id"

        if hit.album_id is not None:
            album = self._by_type_and_id.get(("album", hit.album_id))
            if album:
                return album, "album id"

        artist_key = normalize_text(hit.artist)
        title_key = normalize_text(hit.name)
        if artist_key and title_key:
            purchase = self._by_artist_title.get((artist_key, title_key))
            if purchase:
                return purchase, "artist/title"
            album_purchase = self._album_tracks_by_artist_title.get((artist_key, title_key))
            if album_purchase:
                return album_purchase, "album tracklist"

        return None, None

    def match_hit(self, hit: BandcampSearchHit) -> OwnershipMatch:
        purchase, match_type = self.find_purchase_for_hit(hit)
        if not purchase or not match_type:
            return OwnershipMatch(False)
        return match_from_purchase(purchase, match_type)


def normalize_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def match_from_purchase(purchase: BandcampPurchase, match_type: str) -> OwnershipMatch:
    return OwnershipMatch(
        True,
        match_type=match_type,
        purchase_item_id=purchase.item_id,
        redownload_available=purchase.redownload_available,
    )


def _get_fan_id() -> int:
    status = verify_cookie_header()
    if not status.authenticated or status.fan_id is None:
        raise BandcampAuthError(status.message or "Bandcamp is not authenticated")
    return status.fan_id


def _redownload_url_for_item(item: dict[str, Any], redownload_urls: dict[str, str]) -> str | None:
    sale_item_type = item.get("sale_item_type")
    sale_item_id = item.get("sale_item_id")
    if not sale_item_type or sale_item_id is None:
        return None
    return redownload_urls.get(f"{sale_item_type}{sale_item_id}")


def _track_titles_for_item(item: dict[str, Any], tracklists: dict[str, Any]) -> tuple[str, ...]:
    item_id = item.get("item_id")
    item_type = item.get("item_type")
    if item_id is None:
        return ()
    keys = [f"{str(item_type or '')[0]}{item_id}", str(item_id)]
    tracks: Any = None
    for key in keys:
        tracks = tracklists.get(key)
        if tracks:
            break
    if not isinstance(tracks, list):
        return ()
    return tuple(str(track.get("title") or "") for track in tracks if track.get("title"))


def _purchase_from_item(
    item: dict[str, Any],
    redownload_urls: dict[str, str],
    tracklists: dict[str, Any],
) -> BandcampPurchase | None:
    item_id = item.get("item_id")
    if item_id is None:
        return None
    item_type = str(item.get("item_type") or "").strip()
    if item_type not in {"album", "track"}:
        return None
    sale_item_id = item.get("sale_item_id")
    return BandcampPurchase(
        item_id=int(item_id),
        item_type=item_type,
        title=str(item.get("item_title") or item.get("album_title") or ""),
        artist=str(item.get("band_name") or ""),
        album=str(item.get("album_title") or ""),
        sale_item_type=str(item.get("sale_item_type") or ""),
        sale_item_id=int(sale_item_id) if sale_item_id is not None else None,
        redownload_url=_redownload_url_for_item(item, redownload_urls),
        track_titles=_track_titles_for_item(item, tracklists),
    )


def load_collection(max_pages: int = 100, per_page: int = 100) -> BandcampCollection:
    session = authenticated_session()
    fan_id = _get_fan_id()
    token = f"{int(time.time())}:0:a::"
    purchases: list[BandcampPurchase] = []

    for _page in range(max_pages):
        response = session.post(
            COLLECTION_ITEMS_URL,
            json={"fan_id": fan_id, "count": per_page, "older_than_token": token},
            timeout=30,
        )
        if response.status_code != 200:
            raise BandcampCollectionError(
                f"Bandcamp collection returned HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except Exception as exc:
            raise BandcampCollectionError("Bandcamp collection returned invalid JSON") from exc

        items = data.get("items") or []
        if not items:
            break
        redownload_urls = data.get("redownload_urls") or {}
        tracklists = data.get("tracklists") or {}
        for item in items:
            purchase = _purchase_from_item(item, redownload_urls, tracklists)
            if purchase:
                purchases.append(purchase)
            item_token = item.get("token")
            if item_token:
                token = str(item_token)

        if not data.get("more_available"):
            break
        token = str(data.get("last_token") or token)

    return BandcampCollection(purchases)


def get_collection_status() -> str:
    response = authenticated_session().get(BANDCAMP_HOME, timeout=20)
    return f"HTTP {response.status_code}"
