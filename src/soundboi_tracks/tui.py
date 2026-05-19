from __future__ import annotations

import time
import webbrowser

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, LoadingIndicator, RichLog, Static

from soundboi_tracks.providers.bandcamp.auth import (
    BandcampAuthError,
    BandcampAuthStatus,
    BandcampBrowserLogin,
    has_required_auth_cookies,
    open_bandcamp_page_and_wait_for_close,
    verify_cookie_header,
)
from soundboi_tracks.providers.bandcamp.collection import load_collection
from soundboi_tracks.providers.bandcamp.download import (
    BandcampDownloadResult,
    BandcampDownloadError,
    download_purchase,
)
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit
from soundboi_tracks.providers.beatport.search import BeatportDownloadError, download_beatport_track
from soundboi_tracks.providers.search import search_all
from soundboi_tracks.providers.spotify.auth import SpotifyAuthError, load_token, login as spotify_login
from soundboi_tracks.providers.spotify.client import SpotifyClient, SpotifyClientError
from soundboi_tracks.providers.spotify.models import SpotifyPlaylist, SpotifyTrack


class SoundboiTracksApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        padding: 1 2;
        height: 1fr;
    }

    #status {
        margin-bottom: 1;
        padding: 1 2;
        border: round $primary;
    }

    #body {
        height: 1fr;
    }

    #spotify-pane {
        width: 45%;
        margin-right: 1;
    }

    #search-pane {
        width: 55%;
    }

    #bandcamp-buttons,
    #search-row {
        height: auto;
        margin-bottom: 1;
    }

    #search-input {
        width: 1fr;
        margin-right: 1;
    }

    Button {
        margin-right: 1;
    }

    DataTable {
        border: round $surface;
        height: 1fr;
        margin-bottom: 1;
    }

    #results {
        height: 2fr;
    }

    #log {
        border: round $surface;
        height: 1fr;
    }

    #download-status {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
        border: round $warning;
        display: none;
    }

    #download-status-text {
        width: 1fr;
        padding-left: 1;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self._hits: list[BandcampSearchHit] = []
        self._playlists: list[SpotifyPlaylist] = []
        self._spotify_tracks: list[SpotifyTrack] = []
        self._bandcamp_status = "Bandcamp: not checked"
        self._spotify_status = "Spotify: not checked"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main"):
            yield Static("Bandcamp: checking... | Spotify: not checked", id="status")
            with Horizontal(id="body"):
                with Vertical(id="spotify-pane"):
                    yield Static("Spotify Playlists")
                    yield DataTable(id="spotify-playlists")
                    yield Static("Playlist Tracks")
                    yield DataTable(id="spotify-tracks")
                with Vertical(id="search-pane"):
                    with Horizontal(id="bandcamp-buttons"):
                        yield Button("Login to Bandcamp", id="bandcamp-login", variant="primary")
                        yield Button("Check Auth", id="bandcamp-check")
                    with Horizontal(id="search-row"):
                        yield Input(placeholder="Search: artist track", id="search-input")
                        yield Button("Search", id="search", variant="success")
                        yield Button("Open Page", id="open-page")
                        yield Button("Refresh Search", id="refresh-purchases")
                        yield Button("Download Selected", id="download-selected", variant="warning")
                    yield DataTable(id="results")
                    with Horizontal(id="download-status"):
                        yield LoadingIndicator(id="download-spinner")
                        yield Static("Idle", id="download-status-text")
                    yield RichLog(id="log", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self._log("Welcome. Spotify is on the left; combined Bandcamp/Beatport search is on the right.")
        self._log("Click a Spotify playlist to load tracks; click a track to search the other backends.")
        self._setup_tables()
        self._check_auth()
        self._bootstrap_spotify()

    def _setup_tables(self) -> None:
        results = self.query_one("#results", DataTable)
        results.cursor_type = "row"
        results.add_column("#", width=4)
        results.add_column("Source", width=9)
        results.add_column("Type", width=8)
        results.add_column("Track", width=32)
        results.add_column("Artist", width=26)
        results.add_column("Album", width=24)
        results.add_column("URL", width=20)

        playlists = self.query_one("#spotify-playlists", DataTable)
        playlists.cursor_type = "row"
        playlists.add_columns("#", "Access", "Playlist", "Owner", "Tracks")

        tracks = self.query_one("#spotify-tracks", DataTable)
        tracks.cursor_type = "row"
        tracks.add_column("#", width=4)
        tracks.add_column("Track", width=28)
        tracks.add_column("Artist", width=22)
        tracks.add_column("Album", width=24)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bandcamp-login":
            self.query_one("#bandcamp-login", Button).disabled = True
            self._log("Opening Bandcamp login browser...")
            self.run_worker(self._login_worker, thread=True)
        elif event.button.id == "bandcamp-check":
            self._check_auth()
        elif event.button.id == "search":
            self._start_search()
        elif event.button.id == "open-page":
            self._open_selected_page()
        elif event.button.id == "refresh-purchases":
            self._refresh_purchases()
        elif event.button.id == "download-selected":
            self._download_selected()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._start_search()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "spotify-playlists":
            self._load_selected_spotify_playlist_tracks()
        elif event.data_table.id == "spotify-tracks":
            self._search_selected_spotify_track()

    def _login_worker(self) -> None:
        deadline = time.monotonic() + 300
        last_cookie_names: tuple[str, ...] = ()
        try:
            with BandcampBrowserLogin() as login:
                self.call_from_thread(self._log, "Browser opened. Log in manually; auth will auto-complete.")
                while time.monotonic() < deadline:
                    captured = login.capture()
                    if captured.names != last_cookie_names:
                        last_cookie_names = captured.names
                        names = ", ".join(captured.names) if captured.names else "none"
                        self.call_from_thread(self._log, f"Bandcamp cookie names seen: {names}")

                    if has_required_auth_cookies(captured.header):
                        status = login.finish()
                        self.call_from_thread(self._render_status, status)
                        if status.authenticated:
                            self.call_from_thread(self._log, "Bandcamp login complete.")
                            return
                        self.call_from_thread(self._log, f"Login not complete yet: {status.message}")

                    time.sleep(1)

            self.call_from_thread(self._log, "Bandcamp login timed out after 5 minutes.")
        except BandcampAuthError as exc:
            self.call_from_thread(self._log, f"Bandcamp login failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected login error: {exc}")
        finally:
            self.call_from_thread(self._reset_login_buttons)

    def _reset_login_buttons(self) -> None:
        self.query_one("#bandcamp-login", Button).disabled = False

    def _check_auth(self) -> None:
        self._log("Checking saved Bandcamp auth...")
        status = verify_cookie_header()
        self._render_status(status)

    def _bootstrap_spotify(self) -> None:
        if load_token():
            self._set_spotify_status("Spotify: loading playlists")
            self._log("Loading Spotify playlists from saved auth...")
            self.run_worker(self._load_spotify_playlists_worker, thread=True)
            return
        self._set_spotify_status("Spotify: logging in")
        self._log("No saved Spotify auth found; opening Spotify login...")
        self.run_worker(self._spotify_login_worker, thread=True)

    def _spotify_login_worker(self) -> None:
        try:
            path = spotify_login()
        except SpotifyAuthError as exc:
            self.call_from_thread(self._log, f"Spotify login failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected Spotify login error: {exc}")
        else:
            self.call_from_thread(self._log, f"Spotify login complete. Token file: {path}")
            self.call_from_thread(self._set_spotify_status, "Spotify: loading playlists")
            self._load_spotify_playlists_worker()

    def _load_spotify_playlists_worker(self) -> None:
        try:
            playlists = SpotifyClient().list_playlists()
        except (SpotifyAuthError, SpotifyClientError) as exc:
            self.call_from_thread(self._log, f"Spotify playlists failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected Spotify playlist error: {exc}")
        else:
            self.call_from_thread(self._render_spotify_playlists, playlists)
            self.call_from_thread(self._log, f"Loaded {len(playlists)} Spotify playlist(s).")
            self.call_from_thread(self._set_spotify_status, "Spotify: playlists loaded")

    def _load_selected_spotify_playlist_tracks(self) -> None:
        playlist = self._selected_spotify_playlist()
        if not playlist:
            return
        if not playlist.accessible:
            self._log(
                "Spotify only allows track loading for playlists you own or collaborate on. "
                f"Selected owner: {playlist.owner}."
            )
            return
        self.query_one("#spotify-tracks", DataTable).clear()
        self._log(f"Loading Spotify tracks from: {playlist.name}")
        self.run_worker(lambda: self._load_spotify_tracks_worker(playlist), thread=True)

    def _load_spotify_tracks_worker(self, playlist: SpotifyPlaylist) -> None:
        try:
            tracks = SpotifyClient().list_playlist_tracks(playlist.playlist_id)
        except (SpotifyAuthError, SpotifyClientError) as exc:
            self.call_from_thread(self._log, f"Spotify tracks failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected Spotify tracks error: {exc}")
        else:
            self.call_from_thread(self._render_spotify_tracks, tracks)
            self.call_from_thread(self._log, f"Loaded {len(tracks)} Spotify track(s).")

    def _start_search(self, query: str | None = None) -> None:
        if query is None:
            query = self.query_one("#search-input", Input).value.strip()
        if not query:
            self._log("Enter a search query first.")
            return
        self.query_one("#search", Button).disabled = True
        self.query_one("#search-input", Input).value = query
        self._log(f"Searching providers for: {query}")
        self.run_worker(lambda: self._search_worker(query), thread=True)

    def _refresh_purchases(self) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if not query:
            self._log("Run a search first, then refresh it.")
            return
        self._log("Refreshing current search...")
        self._start_search(query)

    def _search_selected_spotify_track(self) -> None:
        track = self._selected_spotify_track()
        if not track:
            return
        self._log(f"Searching selected Spotify track: {track.artist_label} - {track.name}")
        self._start_search(track.query)

    def _selected_hit(self) -> BandcampSearchHit | None:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._hits):
            self._log("Select a search result first.")
            return None
        return self._hits[row]

    def _selected_spotify_playlist(self) -> SpotifyPlaylist | None:
        table = self.query_one("#spotify-playlists", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._playlists):
            self._log("Select a Spotify playlist first.")
            return None
        return self._playlists[row]

    def _selected_spotify_track(self) -> SpotifyTrack | None:
        table = self.query_one("#spotify-tracks", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._spotify_tracks):
            self._log("Select a Spotify track first.")
            return None
        return self._spotify_tracks[row]

    def _open_selected_page(self) -> None:
        hit = self._selected_hit()
        if not hit:
            return
        if not hit.url:
            self._log("Selected result has no URL.")
            return
        webbrowser.open(hit.url)
        self._log(f"Opened {hit.source} page: {hit.url}")

    def _download_selected(self) -> None:
        hit = self._selected_hit()
        if not hit:
            return
        self.query_one("#download-selected", Button).disabled = True
        message = (
            f"Preparing {hit.source}: {hit.artist} - {hit.name}"
            if hit.artist
            else f"Preparing {hit.source}: {hit.name}"
        )
        self._show_download_status(message)
        self._log(message)
        self.run_worker(lambda: self._download_worker(hit), thread=True)

    def _search_worker(self, query: str) -> None:
        try:
            result = search_all(query)
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected search error: {exc}")
        else:
            self.call_from_thread(self._render_results, result.hits)
            for error in result.errors:
                self.call_from_thread(self._log, f"Provider search failed: {error}")
            self.call_from_thread(self._log, f"Found {len(result.hits)} combined result(s).")
        finally:
            self.call_from_thread(self._enable_search_button)

    def _enable_search_button(self) -> None:
        self.query_one("#search", Button).disabled = False

    def _download_worker(self, hit: BandcampSearchHit) -> None:
        try:
            if hit.source == "bandcamp":
                result = self._download_bandcamp_with_purchase_flow(hit)
                if result is None:
                    message = "Bandcamp purchase was not detected; no download started."
                    self.call_from_thread(self._log, message)
                    return
                message = (
                    f"Downloaded {len(result.files)} file(s) as {result.actual_format} "
                    f"to {result.output_dir}"
                )
            elif hit.source == "beatport" and hit.item_id is not None:
                result = download_beatport_track(hit.item_id)
                message = f"Started Beatport download via Orpheus into {result.output_dir}"
            else:
                raise BandcampDownloadError(f"Download is not supported for {hit.source}")
        except (BandcampDownloadError, BeatportDownloadError) as exc:
            self.call_from_thread(self._log, f"Download failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected download error: {exc}")
        else:
            self.call_from_thread(self._log, message)
        finally:
            self.call_from_thread(self._enable_download_button)
            self.call_from_thread(self._hide_download_status)

    def _download_bandcamp_with_purchase_flow(
        self, hit: BandcampSearchHit
    ) -> BandcampDownloadResult | None:
        self.call_from_thread(self._show_download_status, "Checking Bandcamp purchases...")
        collection = load_collection()
        purchase, match_type = collection.find_purchase_for_hit(hit)

        if not purchase:
            if not hit.url:
                raise BandcampDownloadError("Bandcamp result has no purchase page URL")
            self.call_from_thread(
                self._show_download_status,
                "Opening Bandcamp purchase page. Close the browser when finished.",
            )
            self.call_from_thread(self._log, f"Opening Bandcamp purchase page: {hit.url}")
            open_bandcamp_page_and_wait_for_close(hit.url)
            self.call_from_thread(self._show_download_status, "Checking Bandcamp purchases again...")
            collection = load_collection()
            purchase, match_type = collection.find_purchase_for_hit(hit)

        if not purchase:
            return None
        if not purchase.redownload_url:
            raise BandcampDownloadError(
                f"Matched purchase by {match_type}, but Bandcamp did not provide a redownload URL"
            )

        self.call_from_thread(self._show_download_status, "Downloading Bandcamp purchase...")
        return download_purchase(purchase)

    def _enable_download_button(self) -> None:
        self.query_one("#download-selected", Button).disabled = False

    def _show_download_status(self, message: str) -> None:
        self.query_one("#download-status", Horizontal).display = True
        self.query_one("#download-status-text", Static).update(message)

    def _hide_download_status(self) -> None:
        self.query_one("#download-status-text", Static).update("Idle")
        self.query_one("#download-status", Horizontal).display = False

    def _render_results(self, hits: list[BandcampSearchHit]) -> None:
        self._hits = hits
        table = self.query_one("#results", DataTable)
        table.clear()
        for hit in hits:
            table.add_row(
                str(hit.rank),
                hit.source,
                hit.result_type,
                hit.name,
                hit.artist,
                hit.album,
                hit.url,
            )

    def _render_spotify_playlists(self, playlists: list[SpotifyPlaylist]) -> None:
        self._playlists = playlists
        table = self.query_one("#spotify-playlists", DataTable)
        table.clear()
        for index, playlist in enumerate(playlists, start=1):
            table.add_row(
                str(index),
                "yes" if playlist.accessible else "no",
                playlist.name,
                playlist.owner,
                str(playlist.track_count),
            )

    def _render_spotify_tracks(self, tracks: list[SpotifyTrack]) -> None:
        self._spotify_tracks = tracks
        table = self.query_one("#spotify-tracks", DataTable)
        table.clear()
        for index, track in enumerate(tracks, start=1):
            table.add_row(str(index), track.name, track.artist_label, track.album)

    def _render_status(self, status: BandcampAuthStatus) -> None:
        bandcamp_label = "Bandcamp: not logged in"
        if status.authenticated:
            bandcamp_label = f"Bandcamp: logged in as fan_id {status.fan_id}"
            if status.username:
                bandcamp_label += f" ({status.username})"
            self._log(f"Authenticated. Cookie file: {status.cookie_path}")
        else:
            self._log(f"Not authenticated: {status.message}")
        self._set_bandcamp_status(bandcamp_label)

    def _set_bandcamp_status(self, label: str) -> None:
        self._bandcamp_status = label
        self._render_combined_status()

    def _set_spotify_status(self, label: str) -> None:
        self._spotify_status = label
        self._render_combined_status()

    def _render_combined_status(self) -> None:
        self.query_one("#status", Static).update(
            f"{self._bandcamp_status} | {self._spotify_status}"
        )

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)


def run() -> None:
    SoundboiTracksApp().run()
