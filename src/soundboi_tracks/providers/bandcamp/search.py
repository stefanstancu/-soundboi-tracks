from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from curl_cffi import requests


BANDCAMP_SEARCH_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"
TYPE_LABELS = {
    "a": "album",
    "b": "artist",
    "f": "fan",
    "l": "label",
    "t": "track",
}


class BandcampSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class BandcampSearchHit:
    source: str
    rank: int
    result_type: str
    name: str
    artist: str
    album: str
    url: str
    item_id: int | None = None
    album_id: int | None = None
    band_id: int | None = None
    owned: bool | None = None
    ownership_match: str | None = None
    purchase_item_id: int | None = None
    redownload_available: bool | None = None

    @property
    def owned_label(self) -> str:
        if self.owned is True:
            return "yes" if self.redownload_available else "yes*"
        if self.owned is False:
            return "no"
        return "unknown"

    def with_ownership(
        self,
        owned: bool,
        ownership_match: str | None = None,
        purchase_item_id: int | None = None,
        redownload_available: bool = False,
    ) -> BandcampSearchHit:
        return BandcampSearchHit(
            source=self.source,
            rank=self.rank,
            result_type=self.result_type,
            name=self.name,
            artist=self.artist,
            album=self.album,
            url=self.url,
            item_id=self.item_id,
            album_id=self.album_id,
            band_id=self.band_id,
            owned=owned,
            ownership_match=ownership_match,
            purchase_item_id=purchase_item_id,
            redownload_available=redownload_available,
        )


def normalize_bandcamp_url(url: str) -> str:
    if not url:
        return ""
    second_scheme_index = url.find("https://", len("https://"))
    if second_scheme_index != -1:
        return url[second_scheme_index:]
    return url


def _type_label(value: str | None) -> str:
    if not value:
        return "unknown"
    return TYPE_LABELS.get(value, value)


def _hit_from_result(result: dict[str, Any], rank: int) -> BandcampSearchHit:
    return BandcampSearchHit(
        source="bandcamp",
        rank=rank,
        result_type=_type_label(result.get("type")),
        name=str(result.get("name") or ""),
        artist=str(result.get("band_name") or ""),
        album=str(result.get("album_name") or ""),
        url=normalize_bandcamp_url(str(result.get("url") or "")),
        item_id=result.get("id"),
        album_id=result.get("album_id"),
        band_id=result.get("band_id"),
    )


def search_bandcamp(query: str, limit: int = 15) -> list[BandcampSearchHit]:
    query = query.strip()
    if not query:
        return []

    try:
        response = requests.get(
            BANDCAMP_SEARCH_URL,
            params={"q": query, "param_with_locations": "true"},
            impersonate="chrome",
            timeout=20,
        )
    except Exception as exc:
        raise BandcampSearchError(f"Bandcamp search request failed: {exc}") from exc

    if response.status_code != 200:
        raise BandcampSearchError(f"Bandcamp search returned HTTP {response.status_code}")

    try:
        data = response.json()
    except Exception as exc:
        raise BandcampSearchError("Bandcamp search returned invalid JSON") from exc

    results = data.get("results")
    if not isinstance(results, list):
        raise BandcampSearchError("Bandcamp search response did not include results")

    return [_hit_from_result(result, rank) for rank, result in enumerate(results[:limit], start=1)]


def search_bandcamp_with_collection(query: str, limit: int = 15) -> list[BandcampSearchHit]:
    from soundboi_tracks.providers.bandcamp.auth import verify_cookie_header
    from soundboi_tracks.providers.bandcamp.collection import load_collection

    hits = search_bandcamp(query, limit=limit)
    if not hits:
        return hits

    status = verify_cookie_header()
    if not status.authenticated:
        return hits

    collection = load_collection()
    annotated = []
    for hit in hits:
        match = collection.match_hit(hit)
        annotated.append(
            hit.with_ownership(
                owned=match.owned,
                ownership_match=match.match_type,
                purchase_item_id=match.purchase_item_id,
                redownload_available=match.redownload_available,
            )
        )
    return annotated
