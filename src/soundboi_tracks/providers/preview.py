from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape

from bs4 import BeautifulSoup
from curl_cffi import requests

from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit
from soundboi_tracks.providers.beatport.search import BeatportClient


class PreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreviewStream:
    url: str
    title: str
    source: str


def get_preview_stream(hit: BandcampSearchHit) -> PreviewStream:
    if hit.source == "beatport":
        return _get_beatport_preview(hit)
    if hit.source == "bandcamp":
        return _get_bandcamp_preview(hit)
    raise PreviewError(f"Preview is not supported for {hit.source}")


def _get_beatport_preview(hit: BandcampSearchHit) -> PreviewStream:
    if hit.item_id is None:
        raise PreviewError("Beatport result has no track id")
    url = BeatportClient().get_track_preview_url(hit.item_id)
    return PreviewStream(url=url, title=preview_title(hit), source="beatport")


def _get_bandcamp_preview(hit: BandcampSearchHit) -> PreviewStream:
    if not hit.url:
        raise PreviewError("Bandcamp result has no URL")
    try:
        response = requests.get(hit.url, impersonate="chrome", timeout=30)
    except Exception as exc:
        raise PreviewError(f"Could not fetch Bandcamp page: {exc}") from exc
    if response.status_code != 200:
        raise PreviewError(f"Bandcamp page returned HTTP {response.status_code}")

    data = _extract_tralbum_data(response.text)
    trackinfo = data.get("trackinfo")
    if not isinstance(trackinfo, list):
        raise PreviewError("Bandcamp page did not include track preview data")

    stream_url = _select_bandcamp_stream(trackinfo, hit)
    if not stream_url:
        raise PreviewError("Bandcamp did not expose a playable preview stream")
    return PreviewStream(url=stream_url, title=preview_title(hit), source="bandcamp")


def _extract_tralbum_data(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find(attrs={"data-tralbum": True})
    if tag and tag.attrs.get("data-tralbum"):
        blob = str(tag.attrs["data-tralbum"])
    else:
        match = re.search(r'data-tralbum="([^"]+)"', html)
        if not match:
            raise PreviewError("Could not find Bandcamp tralbum data")
        blob = match.group(1)
    try:
        data = json.loads(unescape(blob))
    except json.JSONDecodeError as exc:
        raise PreviewError("Bandcamp tralbum data was invalid JSON") from exc
    if not isinstance(data, dict):
        raise PreviewError("Bandcamp tralbum data was not an object")
    return data


def _select_bandcamp_stream(trackinfo: list[object], hit: BandcampSearchHit) -> str | None:
    fallback: str | None = None
    for item in trackinfo:
        if not isinstance(item, dict):
            continue
        file_data = item.get("file")
        if not isinstance(file_data, dict):
            continue
        stream_url = file_data.get("mp3-128")
        if not stream_url:
            continue
        if fallback is None:
            fallback = str(stream_url)
        item_id = item.get("track_id") or item.get("id")
        if hit.item_id is not None and str(item_id) == str(hit.item_id):
            return str(stream_url)
        title = str(item.get("title") or "")
        if title and title.casefold() == hit.name.casefold():
            return str(stream_url)
    return fallback


def preview_title(hit: BandcampSearchHit) -> str:
    return f"{hit.artist} - {hit.name}" if hit.artist else hit.name
