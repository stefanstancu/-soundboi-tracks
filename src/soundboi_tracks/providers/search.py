from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import zip_longest

from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit, search_bandcamp
from soundboi_tracks.providers.beatport.search import search_beatport


@dataclass(frozen=True)
class ProviderSearchResult:
    hits: list[BandcampSearchHit]
    errors: tuple[str, ...] = ()


def search_all(query: str, limit_per_provider: int = 15) -> ProviderSearchResult:
    providers = {
        "bandcamp": lambda: search_bandcamp(query, limit=limit_per_provider),
        "beatport": lambda: search_beatport(query, limit=limit_per_provider),
    }
    hits_by_provider: dict[str, list[BandcampSearchHit]] = {name: [] for name in providers}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        future_map = {executor.submit(search): name for name, search in providers.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                hits_by_provider[name] = future.result()
            except Exception as exc:
                errors.append(f"{name}: {exc}")

    combined = interleave_results([hits_by_provider["bandcamp"], hits_by_provider["beatport"]])
    return ProviderSearchResult(hits=combined, errors=tuple(errors))


def interleave_results(result_sets: list[list[BandcampSearchHit]]) -> list[BandcampSearchHit]:
    combined = []
    for group in zip_longest(*result_sets):
        for hit in group:
            if hit is not None:
                combined.append(hit)
    return combined
