from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile, is_zipfile

from bs4 import BeautifulSoup

from soundboi_tracks.config import library_incoming_dir
from soundboi_tracks.providers.bandcamp.auth import authenticated_session
from soundboi_tracks.providers.bandcamp.collection import BandcampPurchase, load_collection
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit


FORMAT_PRIORITY = ("mp3-320", "flac", "wav", "aiff-lossless", "alac")


class BandcampDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class BandcampDownloadResult:
    purchase: BandcampPurchase
    requested_format: str
    actual_format: str
    output_dir: Path
    files: tuple[Path, ...]


def download_hit(
    hit: BandcampSearchHit,
    output_root: Path | None = None,
    preferred_format: str = "mp3-320",
) -> BandcampDownloadResult:
    collection = load_collection()
    purchase, match_type = collection.find_purchase_for_hit(hit)
    if not purchase:
        raise BandcampDownloadError("Selected Bandcamp result is not in your purchased collection")
    if not purchase.redownload_url:
        raise BandcampDownloadError(
            f"Matched purchase by {match_type}, but Bandcamp did not provide a redownload URL"
        )
    return download_purchase(purchase, output_root=output_root, preferred_format=preferred_format)


def download_purchase(
    purchase: BandcampPurchase,
    output_root: Path | None = None,
    preferred_format: str = "mp3-320",
) -> BandcampDownloadResult:
    if not purchase.redownload_url:
        raise BandcampDownloadError("Purchase does not have a redownload URL")

    session = authenticated_session()
    page_response = session.get(purchase.redownload_url, timeout=30)
    if page_response.status_code != 200:
        raise BandcampDownloadError(
            f"Bandcamp redownload page returned HTTP {page_response.status_code}"
        )

    page_data = _extract_download_page_data(page_response.text)
    download_url, actual_format = _select_download_url(page_data, purchase, preferred_format)
    download_url = _check_statdownload(session, download_url)

    output_dir = _purchase_output_dir(output_root or library_incoming_dir(), purchase)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w+b", delete=False, dir=output_dir) as temp_file:
        temp_path = Path(temp_file.name)
        _stream_download(session, download_url, temp_file)

    try:
        files = _materialize_download(temp_path, output_dir, purchase, actual_format)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return BandcampDownloadResult(
        purchase=purchase,
        requested_format=preferred_format,
        actual_format=actual_format,
        output_dir=output_dir,
        files=tuple(files),
    )


def _extract_download_page_data(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("div", id="pagedata")
    if not tag:
        raise BandcampDownloadError("Could not find Bandcamp download page data")
    blob = tag.attrs.get("data-blob")
    if not blob:
        raise BandcampDownloadError("Bandcamp download page data was empty")
    try:
        data = json.loads(unescape(blob))
    except json.JSONDecodeError as exc:
        raise BandcampDownloadError("Bandcamp download page data was invalid JSON") from exc
    if not isinstance(data, dict):
        raise BandcampDownloadError("Bandcamp download page data was not an object")
    return data


def _select_download_url(
    page_data: dict[str, object],
    purchase: BandcampPurchase,
    preferred_format: str,
) -> tuple[str, str]:
    digital_items = page_data.get("digital_items")
    if not isinstance(digital_items, list):
        raise BandcampDownloadError("Bandcamp download page did not include digital items")

    selected_item: dict[str, object] | None = None
    for item in digital_items:
        if isinstance(item, dict) and item.get("item_id") == purchase.item_id:
            selected_item = item
            break
    if selected_item is None:
        for item in digital_items:
            if isinstance(item, dict):
                selected_item = item
                break
    if selected_item is None:
        raise BandcampDownloadError("No downloadable digital item found")

    downloads = selected_item.get("downloads")
    if not isinstance(downloads, dict):
        raise BandcampDownloadError("Selected Bandcamp item did not include downloads")

    choices = (preferred_format, *[fmt for fmt in FORMAT_PRIORITY if fmt != preferred_format])
    for fmt in choices:
        details = downloads.get(fmt)
        if isinstance(details, dict) and details.get("url"):
            return str(details["url"]), fmt

    available = ", ".join(sorted(str(fmt) for fmt in downloads.keys()))
    raise BandcampDownloadError(f"No supported download format found. Available: {available}")


def _check_statdownload(session: object, download_url: str) -> str:
    parts = urlsplit(download_url)
    path_parts = parts.path.split("/")
    if len(path_parts) > 1 and path_parts[1] == "download":
        path_parts[1] = "statdownload"
    stat_url = urlunsplit((parts.scheme, parts.netloc, "/".join(path_parts), parts.query, ""))

    response = session.get(stat_url, timeout=30)
    if response.status_code != 200:
        return download_url
    body = response.text.strip()
    if body == "var _statDL_result = { result: 'ok'};":
        return download_url
    for key, value in re.findall(r'"([^"]+)":"([^"]+)"', body):
        if key == "download_url":
            return value
    return download_url


def _stream_download(session: object, url: str, target: object) -> None:
    response = session.get(url, stream=True, timeout=120)
    try:
        if response.status_code != 200:
            raise BandcampDownloadError(f"Download returned HTTP {response.status_code}")
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type == "text/html":
            raise BandcampDownloadError("Download returned HTML instead of audio/archive data")
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                target.write(chunk)
    finally:
        response.close()


def _materialize_download(
    temp_path: Path,
    output_dir: Path,
    purchase: BandcampPurchase,
    actual_format: str,
) -> list[Path]:
    if is_zipfile(temp_path):
        try:
            with ZipFile(temp_path) as archive:
                archive.extractall(output_dir)
                return [output_dir / name for name in archive.namelist() if not name.endswith("/")]
        except BadZipFile as exc:
            raise BandcampDownloadError("Downloaded file looked like a ZIP but could not be opened") from exc

    extension = _extension_for_format(actual_format)
    file_name = f"{safe_filename(purchase.title or 'Bandcamp Track')}.{extension}"
    target = unique_path(output_dir / file_name)
    shutil.move(str(temp_path), target)
    return [target]


def _purchase_output_dir(output_root: Path, purchase: BandcampPurchase) -> Path:
    return output_root


def _extension_for_format(fmt: str) -> str:
    return {
        "aiff-lossless": "aiff",
        "mp3-320": "mp3",
        "mp3-v0": "mp3",
    }.get(fmt, fmt)


def safe_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    return value or "untitled"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise BandcampDownloadError(f"Could not find unique filename for {path}")
