from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Input, LoadingIndicator, RichLog, Static

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
from soundboi_tracks.library.index import LibraryIndex, SearchOrigin
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit
from soundboi_tracks.providers.beatport.search import (
    BeatportDownloadError,
    BeatportSearchError,
    download_beatport_track,
    load_beatport_credentials,
)
from soundboi_tracks.providers.search import search_all
from soundboi_tracks.providers.spotify.auth import SpotifyAuthError, load_token, login as spotify_login
from soundboi_tracks.providers.spotify.client import SpotifyClient, SpotifyClientError
from soundboi_tracks.providers.spotify.models import SpotifyPlaylist, SpotifyTrack


class SoundboiTracksApp(App[None]):
    SPLASH_TITLE = "S O U N D B O I   T R A C K S"
    SPLASH_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    MIN_SPLASH_SECONDS = 3.0

    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        padding: 1 2;
        height: 1fr;
    }

    #splash {
        height: 1fr;
        align: center middle;
        padding: 2 4;
    }

    #splash-card {
        width: auto;
        height: auto;
        align: center middle;
    }

    #splash-title {
        width: auto;
        color: $accent;
        text-style: bold;
        content-align: center middle;
        text-align: center;
    }

    #splash-status {
        width: auto;
        padding-top: 1;
        color: $text-muted;
        content-align: center middle;
        text-align: center;
    }

    #splash-status-row {
        width: auto;
        height: auto;
        align: center middle;
    }

    #splash-spinner {
        width: auto;
        padding-top: 1;
        padding-left: 1;
        color: $accent;
        content-align: center middle;
        text-align: center;
    }

    #app-shell {
        height: 1fr;
        display: none;
    }

    #provider-bar {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
        background: $surface;
    }

    .provider-pill {
        width: auto;
        margin-right: 1;
    }

    .provider-ok {
        color: $success;
    }

    .provider-warn {
        color: $warning;
    }

    .provider-error {
        color: $error;
    }

    .provider-checking {
        color: $text-muted;
    }

    .provider-login {
        margin-right: 2;
    }

    #body {
        height: 1fr;
    }

    #spotify-pane {
        width: 45%;
        height: 1fr;
        margin-right: 1;
    }

    #search-pane {
        width: 55%;
        height: 1fr;
    }

    #spotify-nav {
        height: auto;
        margin-bottom: 1;
    }

    #spotify-title {
        width: 1fr;
    }

    #spotify-content {
        height: 1fr;
    }

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

    #spotify-browser {
        height: 1fr;
    }

    #results {
        height: 2fr;
    }

    .table-loading {
        height: 1fr;
        align: center middle;
        border: round $surface;
        display: none;
        margin-bottom: 1;
    }

    .table-loading-text {
        width: auto;
        padding-top: 1;
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
        self._visible_spotify_tracks: list[SpotifyTrack] = []
        self._spotify_mode = "playlists"
        self._hide_local_spotify_tracks = False
        self._current_playlist: SpotifyPlaylist | None = None
        self._library_index = LibraryIndex()
        self._current_search_origin: SearchOrigin | None = None
        self._current_search_query = ""
        self._startup_complete = False
        self._splash_started_at = time.monotonic()
        self._splash_finished = False
        self._splash_spinner_timer = None
        self._splash_spinner_frame = 0
        self._provider_states = {
            "spotify": ("Spotify Checking", "checking", False),
            "bandcamp": ("Bandcamp Checking", "checking", False),
            "beatport": ("Beatport Checking", "checking", False),
        }

    def compose(self) -> ComposeResult:
        with Container(id="main"):
            with Vertical(id="splash"):
                with Vertical(id="splash-card"):
                    yield Static(self.SPLASH_TITLE, id="splash-title")
                    with Horizontal(id="splash-status-row"):
                        yield Static("warming up providers", id="splash-status")
                        yield Static("⠋", id="splash-spinner")
            with Vertical(id="app-shell"):
                with Horizontal(id="provider-bar"):
                    yield Static("● Spotify Checking", id="spotify-provider", classes="provider-pill provider-checking")
                    yield Button("Login Spotify", id="spotify-login", classes="provider-login")
                    yield Static("● Bandcamp Checking", id="bandcamp-provider", classes="provider-pill provider-checking")
                    yield Button("Login Bandcamp", id="bandcamp-login", classes="provider-login")
                    yield Static("● Beatport Checking", id="beatport-provider", classes="provider-pill provider-checking")
                with Horizontal(id="body"):
                    with Vertical(id="spotify-pane"):
                        with Horizontal(id="spotify-nav"):
                            yield Static("Spotify Playlists", id="spotify-title")
                            yield Button("Back", id="spotify-back")
                            yield Button("Hide Local", id="spotify-local-toggle")
                        with Vertical(id="spotify-content"):
                            yield DataTable(id="spotify-browser")
                            with Vertical(id="spotify-loading", classes="table-loading"):
                                yield LoadingIndicator()
                                yield Static("Loading...", id="spotify-loading-text", classes="table-loading-text")
                    with Vertical(id="search-pane"):
                        with Horizontal(id="search-row"):
                            yield Input(placeholder="Search: artist track", id="search-input")
                            yield Button("Search", id="search", variant="success")
                            yield Button("Download Selected", id="download-selected", variant="warning")
                        yield DataTable(id="results")
                        with Vertical(id="search-loading", classes="table-loading"):
                            yield LoadingIndicator()
                            yield Static("Loading...", id="search-loading-text", classes="table-loading-text")
                        with Horizontal(id="download-status"):
                            yield LoadingIndicator(id="download-spinner")
                            yield Static("Idle", id="download-status-text")
                        yield RichLog(id="log", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self._splash_started_at = time.monotonic()
        self._setup_tables()
        self._render_all_provider_states()
        self._start_splash_spinner()
        self.run_worker(self._run_startup_checks_worker, thread=True)

    def _run_startup_checks_worker(self) -> None:
        self.call_from_thread(
            self._log,
            "Welcome. Spotify is on the left; combined Bandcamp/Beatport search is on the right.",
        )
        self.call_from_thread(
            self._log,
            "Click a Spotify playlist to load tracks; click a track to search the other backends.",
        )
        self._scan_library_for_startup()
        self._check_auth_for_startup()
        self._check_beatport_for_startup()
        self._bootstrap_spotify_for_startup()

    def _scan_library_for_startup(self) -> None:
        try:
            self._library_index.scan()
        except Exception as exc:
            self.call_from_thread(self._log, f"Library index scan failed: {exc}")
        else:
            self.call_from_thread(self._log, "Library index ready.")
            self.call_from_thread(self._refresh_local_indicators)

    def _check_auth_for_startup(self) -> None:
        self.call_from_thread(self._set_splash_status, "checking Bandcamp")
        status = verify_cookie_header()
        self.call_from_thread(self._render_status, status)

    def _check_beatport_for_startup(self) -> None:
        self.call_from_thread(self._set_splash_status, "checking Beatport")
        try:
            load_beatport_credentials()
        except BeatportSearchError as exc:
            self.call_from_thread(
                self._set_provider_status,
                "beatport",
                "Beatport Needs Config",
                "error",
            )
            self.call_from_thread(self._log, f"Beatport unavailable: {exc}")
        else:
            self.call_from_thread(self._set_provider_status, "beatport", "Beatport Ready", "ok")

    def _bootstrap_spotify_for_startup(self) -> None:
        if load_token():
            self.call_from_thread(self._set_splash_status, "loading Spotify playlists")
            self.call_from_thread(self._set_provider_status, "spotify", "Spotify Loading", "checking")
            self.call_from_thread(self._log, "Loading Spotify playlists from saved auth...")
            self._load_spotify_playlists_worker()
            return
        self.call_from_thread(self._set_splash_status, "Spotify login needed")
        self.call_from_thread(
            self._set_provider_status,
            "spotify",
            "Spotify Needs Login",
            "warn",
            True,
        )
        self.call_from_thread(self._log, "No saved Spotify auth found. Use Login Spotify in the provider bar.")
        self.call_from_thread(self._mark_startup_complete)

    def _set_splash_status(self, status: str) -> None:
        if not self._splash_finished:
            self.query_one("#splash-status", Static).update(status)

    def _scan_library_worker(self) -> None:
        try:
            self._library_index.scan()
        except Exception as exc:
            self.call_from_thread(self._log, f"Library index scan failed: {exc}")
        else:
            self.call_from_thread(self._log, "Library index ready.")
            self.call_from_thread(self._refresh_local_indicators)

    def _start_splash_spinner(self) -> None:
        self._splash_spinner_timer = self.set_interval(0.12, self._tick_splash_spinner)
        self._tick_splash_spinner()

    def _tick_splash_spinner(self) -> None:
        frame = self.SPLASH_SPINNER_FRAMES[self._splash_spinner_frame % len(self.SPLASH_SPINNER_FRAMES)]
        self._splash_spinner_frame += 1
        self.query_one("#splash-spinner", Static).update(frame)

    def _mark_startup_complete(self) -> None:
        if self._startup_complete:
            return
        self._startup_complete = True
        self.query_one("#splash-status", Static).update("ready")
        remaining = self.MIN_SPLASH_SECONDS - (time.monotonic() - self._splash_started_at)
        if remaining <= 0:
            self._finish_splash()
        else:
            self.set_timer(remaining, self._finish_splash)

    def _finish_splash(self) -> None:
        if self._splash_finished:
            return
        self._splash_finished = True
        if self._splash_spinner_timer is not None:
            self._splash_spinner_timer.stop()
            self._splash_spinner_timer = None
        self.query_one("#splash", Vertical).display = False
        self.query_one("#app-shell", Vertical).display = True

    def _setup_tables(self) -> None:
        results = self.query_one("#results", DataTable)
        results.cursor_type = "row"
        results.add_column("#", width=4)
        results.add_column("", width=2)
        results.add_column("Source", width=9)
        results.add_column("Type", width=8)
        results.add_column("Track", width=32)
        results.add_column("Artist", width=26)
        results.add_column("Album", width=24)
        results.add_column("URL", width=20)

        browser = self.query_one("#spotify-browser", DataTable)
        browser.cursor_type = "row"
        self.query_one("#spotify-back", Button).display = False
        self.query_one("#spotify-local-toggle", Button).display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bandcamp-login":
            self.query_one("#bandcamp-login", Button).disabled = True
            self._set_provider_status("bandcamp", "Bandcamp Logging In", "checking", show_login=False)
            self._log("Opening Bandcamp login browser...")
            self.run_worker(self._login_worker, thread=True)
        elif event.button.id == "spotify-login":
            self.query_one("#spotify-login", Button).disabled = True
            self._set_provider_status("spotify", "Spotify Logging In", "checking", show_login=False)
            self._log("Opening Spotify login in your browser...")
            self.run_worker(self._spotify_login_worker, thread=True)
        elif event.button.id == "search":
            self._start_search()
        elif event.button.id == "download-selected":
            self._download_selected()
        elif event.button.id == "spotify-back":
            self._show_spotify_playlists()
        elif event.button.id == "spotify-local-toggle":
            self._toggle_spotify_local_visibility()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._start_search()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "spotify-browser":
            return
        if self._spotify_mode == "playlists":
            self._load_selected_spotify_playlist_tracks()
        elif self._spotify_mode == "tracks":
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
        self.query_one("#splash-status", Static).update("checking Bandcamp")
        status = verify_cookie_header()
        self._render_status(status)

    def _check_beatport_status(self) -> None:
        self.query_one("#splash-status", Static).update("checking Beatport")
        try:
            load_beatport_credentials()
        except BeatportSearchError as exc:
            self._set_provider_status("beatport", "Beatport Needs Config", "error")
            self._log(f"Beatport unavailable: {exc}")
        else:
            self._set_provider_status("beatport", "Beatport Ready", "ok")

    def _bootstrap_spotify(self) -> None:
        if load_token():
            self.query_one("#splash-status", Static).update("loading Spotify playlists")
            self._set_provider_status("spotify", "Spotify Loading", "checking")
            self._log("Loading Spotify playlists from saved auth...")
            self.run_worker(self._load_spotify_playlists_worker, thread=True)
            return
        self.query_one("#splash-status", Static).update("Spotify login needed")
        self._set_provider_status("spotify", "Spotify Needs Login", "warn", show_login=True)
        self._log("No saved Spotify auth found. Use Login Spotify in the provider bar.")
        self._mark_startup_complete()

    def _spotify_login_worker(self) -> None:
        try:
            path = spotify_login()
        except SpotifyAuthError as exc:
            self.call_from_thread(
                self._set_provider_status,
                "spotify",
                "Spotify Needs Login",
                "warn",
                True,
            )
            self.call_from_thread(self._log, f"Spotify login failed: {exc}")
        except Exception as exc:
            self.call_from_thread(
                self._set_provider_status,
                "spotify",
                "Spotify Error",
                "error",
                True,
            )
            self.call_from_thread(self._log, f"Unexpected Spotify login error: {exc}")
        else:
            self.call_from_thread(self._log, f"Spotify login complete. Token file: {path}")
            self.call_from_thread(self._set_provider_status, "spotify", "Spotify Loading", "checking")
            self._load_spotify_playlists_worker()

    def _load_spotify_playlists_worker(self) -> None:
        try:
            playlists = SpotifyClient().list_playlists()
        except (SpotifyAuthError, SpotifyClientError) as exc:
            self.call_from_thread(
                self._set_provider_status,
                "spotify",
                "Spotify Needs Login",
                "warn",
                True,
            )
            self.call_from_thread(self._log, f"Spotify playlists failed: {exc}")
            self.call_from_thread(self._mark_startup_complete)
        except Exception as exc:
            self.call_from_thread(self._set_provider_status, "spotify", "Spotify Error", "error", True)
            self.call_from_thread(self._log, f"Unexpected Spotify playlist error: {exc}")
            self.call_from_thread(self._mark_startup_complete)
        else:
            self.call_from_thread(self._render_spotify_playlists, playlists)
            self.call_from_thread(self._log, f"Loaded {len(playlists)} Spotify playlist(s).")
            self.call_from_thread(self._set_provider_status, "spotify", "Spotify Connected", "ok")
            self.call_from_thread(self._mark_startup_complete)

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
        self._current_playlist = playlist
        self.query_one("#spotify-title", Static).update(f"Spotify: {playlist.name}")
        self.query_one("#spotify-back", Button).display = True
        self._show_table_loading(
            "#spotify-loading",
            "#spotify-loading-text",
            "#spotify-browser",
            f"Loading tracks from {playlist.name}...",
        )
        self._log(f"Loading Spotify tracks from: {playlist.name}")
        self.run_worker(lambda: self._load_spotify_tracks_worker(playlist), thread=True)

    def _load_spotify_tracks_worker(self, playlist: SpotifyPlaylist) -> None:
        try:
            tracks = SpotifyClient().list_playlist_tracks(playlist.playlist_id)
        except (SpotifyAuthError, SpotifyClientError) as exc:
            self.call_from_thread(self._hide_spotify_loading)
            self.call_from_thread(self._log, f"Spotify tracks failed: {exc}")
        except Exception as exc:
            self.call_from_thread(self._hide_spotify_loading)
            self.call_from_thread(self._log, f"Unexpected Spotify tracks error: {exc}")
        else:
            self.call_from_thread(self._render_spotify_tracks, tracks)
            self.call_from_thread(self._log, f"Loaded {len(tracks)} Spotify track(s).")

    def _start_search(self, query: str | None = None, origin: SearchOrigin | None = None) -> None:
        if query is None:
            query = self.query_one("#search-input", Input).value.strip()
            origin = None
        if not query:
            self._log("Enter a search query first.")
            return
        self._current_search_origin = origin
        self._current_search_query = query
        self.query_one("#search", Button).disabled = True
        self.query_one("#search-input", Input).value = query
        self._hits = []
        self.query_one("#results", DataTable).clear()
        self._show_table_loading(
            "#search-loading",
            "#search-loading-text",
            "#results",
            f"Searching Bandcamp + Beatport for {query}...",
        )
        self._log(f"Searching providers for: {query}")
        self.run_worker(lambda: self._search_worker(query), thread=True)

    def _search_selected_spotify_track(self) -> None:
        track = self._selected_spotify_track()
        if not track:
            return
        self._log(f"Searching selected Spotify track: {track.artist_label} - {track.name}")
        origin = SearchOrigin(
            source="spotify",
            spotify_track_id=track.track_id,
            spotify_title=track.name,
            spotify_artists=track.artists,
            spotify_album=track.album,
            spotify_playlist_id=self._current_playlist.playlist_id if self._current_playlist else None,
        )
        self._start_search(track.query, origin=origin)

    def _selected_hit(self) -> BandcampSearchHit | None:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._hits):
            self._log("Select a search result first.")
            return None
        return self._hits[row]

    def _selected_spotify_playlist(self) -> SpotifyPlaylist | None:
        table = self.query_one("#spotify-browser", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._playlists):
            self._log("Select a Spotify playlist first.")
            return None
        return self._playlists[row]

    def _selected_spotify_track(self) -> SpotifyTrack | None:
        table = self.query_one("#spotify-browser", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._visible_spotify_tracks):
            self._log("Select a Spotify track first.")
            return None
        return self._visible_spotify_tracks[row]

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
        origin = self._valid_search_origin()
        self.run_worker(lambda: self._download_worker(hit, origin), thread=True)

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
            self.call_from_thread(self._hide_search_loading)
            self.call_from_thread(self._enable_search_button)

    def _enable_search_button(self) -> None:
        self.query_one("#search", Button).disabled = False

    def _download_worker(self, hit: BandcampSearchHit, origin: SearchOrigin | None) -> None:
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
                self._record_downloaded_files(hit, result.files, origin)
            elif hit.source == "beatport" and hit.item_id is not None:
                result = download_beatport_track(hit.item_id)
                message = f"Downloaded {len(result.files)} Beatport file(s) via Orpheus into {result.output_dir}"
                self._record_downloaded_files(hit, result.files, origin)
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

    def _record_downloaded_files(
        self, hit: BandcampSearchHit, files: tuple[Path, ...], origin: SearchOrigin | None
    ) -> None:
        for file_path in files:
            self._library_index.record_download(file_path, hit, origin=origin)
        self.call_from_thread(self._refresh_local_indicators)

    def _valid_search_origin(self) -> SearchOrigin | None:
        current_query = self.query_one("#search-input", Input).value.strip()
        if current_query != self._current_search_query:
            return None
        return self._current_search_origin

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

    def _show_table_loading(
        self,
        loading_id: str,
        loading_text_id: str,
        table_id: str,
        message: str,
    ) -> None:
        self.query_one(table_id).display = False
        self.query_one(loading_text_id, Static).update(message)
        self.query_one(loading_id).display = True

    def _hide_table_loading(self, loading_id: str, table_id: str) -> None:
        self.query_one(loading_id).display = False
        self.query_one(table_id).display = True

    def _hide_spotify_loading(self) -> None:
        self._hide_table_loading("#spotify-loading", "#spotify-browser")

    def _hide_search_loading(self) -> None:
        self._hide_table_loading("#search-loading", "#results")

    def _render_results(self, hits: list[BandcampSearchHit]) -> None:
        self._hits = hits
        table = self.query_one("#results", DataTable)
        table.clear()
        for hit in hits:
            match = self._library_index.match_hit(hit)
            table.add_row(
                str(hit.rank),
                self._local_marker(match.label),
                hit.source,
                hit.result_type,
                hit.name,
                hit.artist,
                hit.album,
                hit.url,
            )

    def _render_spotify_playlists(self, playlists: list[SpotifyPlaylist]) -> None:
        self._playlists = playlists
        self._show_spotify_playlists()

    def _show_spotify_playlists(self) -> None:
        self._spotify_mode = "playlists"
        self._current_playlist = None
        self._spotify_tracks = []
        self._visible_spotify_tracks = []
        self._hide_spotify_loading()
        self.query_one("#spotify-title", Static).update("Spotify Playlists")
        self.query_one("#spotify-back", Button).display = False
        self.query_one("#spotify-local-toggle", Button).display = False
        table = self.query_one("#spotify-browser", DataTable)
        table.clear()
        table.clear(columns=True)
        table.add_column("#", width=4)
        table.add_column("Access", width=7)
        table.add_column("Playlist", width=30)
        table.add_column("Owner", width=18)
        table.add_column("Tracks", width=7)
        for index, playlist in enumerate(self._playlists, start=1):
            table.add_row(
                str(index),
                "yes" if playlist.accessible else "no",
                playlist.name,
                playlist.owner,
                str(playlist.track_count),
            )

    def _render_spotify_tracks(self, tracks: list[SpotifyTrack]) -> None:
        self._spotify_tracks = tracks
        self._visible_spotify_tracks = self._filtered_spotify_tracks(tracks)
        self._spotify_mode = "tracks"
        self._hide_spotify_loading()
        playlist_name = self._current_playlist.name if self._current_playlist else "Playlist"
        self.query_one("#spotify-title", Static).update(f"Spotify: {playlist_name}")
        self.query_one("#spotify-back", Button).display = True
        toggle = self.query_one("#spotify-local-toggle", Button)
        toggle.display = True
        toggle.label = "Show Local" if self._hide_local_spotify_tracks else "Hide Local"
        table = self.query_one("#spotify-browser", DataTable)
        table.clear()
        table.clear(columns=True)
        table.add_column("#", width=4)
        table.add_column("", width=2)
        table.add_column("Track", width=30)
        table.add_column("Artist", width=24)
        table.add_column("Album", width=26)
        for index, track in enumerate(self._visible_spotify_tracks, start=1):
            match = self._library_index.match_spotify_track(track)
            table.add_row(
                str(index),
                self._local_marker(match.label),
                track.name,
                track.artist_label,
                track.album,
            )

    def _refresh_local_indicators(self) -> None:
        if self._hits:
            self._render_results(self._hits)
        if self._spotify_mode == "tracks" and self._spotify_tracks:
            self._render_spotify_tracks(self._spotify_tracks)

    def _toggle_spotify_local_visibility(self) -> None:
        self._hide_local_spotify_tracks = not self._hide_local_spotify_tracks
        self._render_spotify_tracks(self._spotify_tracks)

    def _filtered_spotify_tracks(self, tracks: list[SpotifyTrack]) -> list[SpotifyTrack]:
        if not self._hide_local_spotify_tracks:
            return tracks
        return [track for track in tracks if self._library_index.match_spotify_track(track).status == "none"]

    def _local_marker(self, label: str) -> Text | str:
        if label == "✓":
            return Text(label, style="green")
        if label == "~":
            return Text(label, style="yellow")
        return label

    def _render_status(self, status: BandcampAuthStatus) -> None:
        if status.authenticated:
            self._set_provider_status("bandcamp", "Bandcamp Connected", "ok", show_login=False)
            self._log(f"Authenticated. Cookie file: {status.cookie_path}")
        else:
            self._set_provider_status("bandcamp", "Bandcamp Needs Login", "warn", show_login=True)
            self._log(f"Not authenticated: {status.message}")

    def _set_provider_status(
        self,
        provider: str,
        label: str,
        kind: str,
        show_login: bool = False,
    ) -> None:
        self._provider_states[provider] = (label, kind, show_login)
        self._render_provider_state(provider)

    def _render_all_provider_states(self) -> None:
        for provider in self._provider_states:
            self._render_provider_state(provider)

    def _render_provider_state(self, provider: str) -> None:
        label, kind, show_login = self._provider_states[provider]
        pill = self.query_one(f"#{provider}-provider", Static)
        pill.update(f"● {label}")
        pill.remove_class("provider-ok", "provider-warn", "provider-error", "provider-checking")
        pill.add_class(f"provider-{kind}")

        button_id = f"#{provider}-login"
        try:
            button = self.query_one(button_id, Button)
        except Exception:
            return
        button.display = show_login
        button.disabled = False

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)


def run() -> None:
    SoundboiTracksApp().run()
