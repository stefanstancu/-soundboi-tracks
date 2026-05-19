from __future__ import annotations

import argparse

from soundboi_tracks.config import (
    library_dir,
    library_index_file,
    spotify_config_file,
    spotify_token_file,
)
from soundboi_tracks.providers.bandcamp.auth import (
    BandcampAuthError,
    login_with_browser,
    verify_cookie_header,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="soundboi-tracks")
    parser.add_argument("--version", action="version", version="soundboi-tracks 0.1.0")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="Check local provider setup")
    subparsers.add_parser("bandcamp-auth", help="Start Bandcamp browser auth flow")
    search_parser = subparsers.add_parser("search", help="Search configured providers")
    search_parser.add_argument("query", nargs="+", help="Search query")
    subparsers.add_parser("tui", help="Open the terminal UI")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "doctor":
        status = verify_cookie_header()
        print("soundboi-tracks project is initialized")
        print(f"Bandcamp cookies: {status.cookie_path}")
        print(f"Bandcamp auth: {'ok' if status.authenticated else status.message}")
        print(f"Downloads: {library_dir()}")
        print(f"Library index: {library_index_file()}")
        print(f"Spotify config: {spotify_config_file()}")
        print(f"Spotify tokens: {spotify_token_file()}")
        return 0

    if args.command == "bandcamp-auth":
        try:
            status = login_with_browser()
        except BandcampAuthError as exc:
            print(f"Bandcamp auth failed: {exc}")
            return 1
        print(f"Bandcamp authenticated as fan_id {status.fan_id}")
        return 0

    if args.command == "tui":
        from soundboi_tracks.tui import run

        run()
        return 0

    if args.command == "search":
        from soundboi_tracks.providers.search import search_all

        result = search_all(" ".join(args.query))
        for error in result.errors:
            print(f"Provider error: {error}")
        for hit in result.hits:
            print(
                f"{hit.rank:>2}. [{hit.source:>8}] [{hit.result_type}] "
                f"{hit.artist} - {hit.name} | {hit.url}"
            )
        return 0

    if args.command is None:
        status = verify_cookie_header()
        if status.authenticated:
            print(f"Bandcamp: logged in as fan_id {status.fan_id}")

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
