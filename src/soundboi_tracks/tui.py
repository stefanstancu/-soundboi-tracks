from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Footer, Input, LoadingIndicator, RichLog, Static

from soundboi_tracks.audio.player import PreviewPlayer, PreviewPlayerError
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
from soundboi_tracks.library.index import LibraryIndex, SearchOrigin, make_artist_title_key
from soundboi_tracks.library.queue import (
    COMPLETED,
    DOWNLOADING,
    FAILED,
    NEEDS_PURCHASE,
    PENDING,
    QueueItem,
    QueueStore,
    queue_id_for_hit,
)
from soundboi_tracks.providers.bandcamp.search import BandcampSearchHit
from soundboi_tracks.providers.preview import PreviewError, get_preview_stream
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
from soundboi_tracks.tui_widgets import PreviewScrubber


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
        height: 2fr;
    }

    #queue-pane {
        height: 1fr;
        margin-top: 1;
    }

    #queue-nav {
        height: auto;
        margin-bottom: 1;
    }

    #queue-title {
        width: 1fr;
    }

    #queue-table {
        height: 1fr;
    }

    #search-row {
        height: auto;
        margin-bottom: 1;
    }

    #preview-row {
        height: auto;
        margin-bottom: 1;
        display: none;
    }

    #preview-title {
        width: 2fr;
    }

    #preview-time {
        width: 16;
        content-align: right middle;
    }

    #preview-progress {
        width: 3fr;
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
        height: 1fr;
    }

    #search-content {
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

    """

    BINDINGS = [("q", "quit", "Quit"), ("p", "preview_selected", "Preview")]

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
        self._queue_store = QueueStore()
        self._queue_items: list[QueueItem] = []
        self._queue_row_by_id: dict[str, int] = {}
        self._queue_status_by_hit: dict[str, str] = {}
        self._queue_status_by_spotify_id: dict[str, str] = {}
        self._search_row_by_queue_id: dict[str, int] = {}
        self._spotify_row_by_track_id: dict[str, int] = {}
        self._queue_animation_timer = None
        self._queue_animation_frame = 0
        self._previewing_hit_key: str | None = None
        self._preview_duration: float | None = None
        self._preview_poll_timer = None
        self._preview_player = PreviewPlayer()
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
                        with Vertical(id="queue-pane"):
                            with Horizontal(id="queue-nav"):
                                yield Static("Queue", id="queue-title")
                                yield Button("Clear Queue", id="queue-clear")
                                yield Button("Download All", id="queue-download-all", variant="warning")
                            yield DataTable(id="queue-table")
                    with Vertical(id="search-pane"):
                        with Horizontal(id="search-row"):
                            yield Input(placeholder="Search: artist track", id="search-input")
                            yield Button("Search", id="search", variant="success")
                            yield Button("Add to Queue", id="add-to-queue")
                            yield Button("Preview", id="preview-selected")
                            yield Button("Download Selected", id="download-selected", variant="warning")
                        with Horizontal(id="preview-row"):
                            yield Static("", id="preview-title")
                            yield PreviewScrubber(id="preview-progress")
                            yield Static("0:00 / 0:00", id="preview-time")
                        with Vertical(id="search-content"):
                            yield DataTable(id="results")
                            with Vertical(id="search-loading", classes="table-loading"):
                                yield LoadingIndicator()
                                yield Static("Loading...", id="search-loading-text", classes="table-loading-text")
                        yield RichLog(id="log", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self._splash_started_at = time.monotonic()
        self._setup_tables()
        self._render_all_provider_states()
        self._start_splash_spinner()
        self.run_worker(self._run_startup_checks_worker, thread=True)

    def on_unmount(self) -> None:
        self._preview_player.stop()

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

        queue = self.query_one("#queue-table", DataTable)
        queue.cursor_type = "row"
        queue.add_column("S", width=2)
        queue.add_column("Source", width=9)
        queue.add_column("Track", width=28)
        queue.add_column("Artist", width=20)
        self._render_queue()

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
        elif event.button.id == "add-to-queue":
            self._add_selected_to_queue()
        elif event.button.id == "preview-selected":
            self._toggle_preview_selected()
        elif event.button.id == "download-selected":
            self._download_selected()
        elif event.button.id == "spotify-back":
            self._show_spotify_playlists()
        elif event.button.id == "spotify-local-toggle":
            self._toggle_spotify_local_visibility()
        elif event.button.id == "queue-download-all":
            self._download_queue()
        elif event.button.id == "queue-clear":
            self._clear_queue()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._start_search()

    def on_preview_scrubber_seek_requested(
        self, message: PreviewScrubber.SeekRequested
    ) -> None:
        self._preview_player.seek(message.seconds)
        self._tick_preview_progress()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "spotify-browser":
            if event.data_table.id == "queue-table":
                self._handle_queue_row_selected()
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

    def _add_selected_to_queue(self) -> None:
        hit = self._selected_hit()
        if not hit:
            return
        item = self._queue_store.add(hit, origin=self._valid_search_origin())
        self._log(f"Queued {item.hit.source}: {item.hit.artist} - {item.hit.name}")
        self._render_queue()
        self._refresh_search_indicators()
        if self._queue_item_affects_visible_spotify(item):
            self._refresh_spotify_indicators()
        if hit.source == "bandcamp":
            self.run_worker(lambda: self._check_queued_bandcamp_ownership(item), thread=True)

    def action_preview_selected(self) -> None:
        self._toggle_preview_selected()

    def _toggle_preview_selected(self) -> None:
        hit = self._selected_hit()
        if not hit:
            return
        hit_key = queue_id_for_hit(hit)
        if self._previewing_hit_key == hit_key:
            self._stop_preview()
            return

        self._stop_preview(log=False)
        self._previewing_hit_key = hit_key
        self.query_one("#preview-selected", Button).disabled = True
        label = f"{hit.artist} - {hit.name}" if hit.artist else hit.name
        self._log(f"Finding preview for {hit.source}: {label}")
        self.run_worker(lambda: self._preview_worker(hit, hit_key), thread=True)

    def _preview_worker(self, hit: BandcampSearchHit, hit_key: str) -> None:
        try:
            preview = get_preview_stream(hit)
            self._preview_player.start(preview.url)
        except (PreviewError, PreviewPlayerError) as exc:
            self.call_from_thread(self._log, f"Preview unavailable: {exc}")
            self.call_from_thread(self._reset_preview_button)
        except Exception as exc:
            self.call_from_thread(self._log, f"Unexpected preview error: {exc}")
            self.call_from_thread(self._reset_preview_button)
        else:
            if self._previewing_hit_key != hit_key:
                self._preview_player.stop()
                return
            self.call_from_thread(self._log, f"Previewing {preview.source}: {preview.title}")
            self.call_from_thread(self.query_one("#preview-title", Static).update, preview.title)
            self.call_from_thread(self._set_preview_playing)

    def _stop_preview(self, log: bool = True) -> None:
        self._preview_player.stop()
        self._previewing_hit_key = None
        self._preview_duration = None
        self._stop_preview_polling()
        self.query_one("#preview-progress", PreviewScrubber).reset()
        self.query_one("#preview-row", Horizontal).display = False
        self._reset_preview_button()
        if log:
            self._log("Stopped preview.")

    def _reset_preview_button(self) -> None:
        self._previewing_hit_key = None
        button = self.query_one("#preview-selected", Button)
        button.disabled = False
        button.label = "Preview"

    def _set_preview_playing(self) -> None:
        button = self.query_one("#preview-selected", Button)
        button.disabled = False
        button.label = "Stop Preview"
        self.query_one("#preview-row", Horizontal).display = True
        self._preview_duration = self._preview_player.duration()
        self._start_preview_polling()

    def _start_preview_polling(self) -> None:
        if self._preview_poll_timer is None:
            self._preview_poll_timer = self.set_interval(0.5, self._tick_preview_progress)
        self._tick_preview_progress()

    def _stop_preview_polling(self) -> None:
        if self._preview_poll_timer is not None:
            self._preview_poll_timer.stop()
            self._preview_poll_timer = None

    def _tick_preview_progress(self) -> None:
        if not self._preview_player.is_playing():
            self._stop_preview(log=False)
            return
        position = self._preview_player.position() or 0
        duration = self._preview_duration or self._preview_player.duration() or 0
        if duration and self._preview_duration is None:
            self._preview_duration = duration
        self.query_one("#preview-progress", PreviewScrubber).set_progress(position, duration)
        self.query_one("#preview-time", Static).update(
            f"{format_time(position)} / {format_time(duration)}"
        )

    def _check_queued_bandcamp_ownership(self, item: QueueItem) -> None:
        try:
            collection = load_collection()
            purchase, _match_type = collection.find_purchase_for_hit(item.hit)
        except Exception as exc:
            self.call_from_thread(self._log, f"Could not check Bandcamp ownership: {exc}")
            return
        if purchase:
            self._queue_store.update_status(item.queue_id, PENDING)
        else:
            self._queue_store.update_status(item.queue_id, NEEDS_PURCHASE, "purchase required")
        self.call_from_thread(self._render_queue)
        self.call_from_thread(self._refresh_queue_indicators_for_item, item)

    def _handle_queue_row_selected(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._queue_items):
            return
        item = self._queue_items[row]
        if item.status != NEEDS_PURCHASE or item.hit.source != "bandcamp":
            return
        self._log(f"Opening Bandcamp purchase page for queued item: {item.hit.artist} - {item.hit.name}")
        self.run_worker(lambda: self._purchase_queued_bandcamp_item(item), thread=True)

    def _purchase_queued_bandcamp_item(self, item: QueueItem) -> None:
        try:
            if not item.hit.url:
                raise BandcampDownloadError("Bandcamp result has no purchase page URL")
            open_bandcamp_page_and_wait_for_close(item.hit.url)
            collection = load_collection()
            purchase, _match_type = collection.find_purchase_for_hit(item.hit)
        except Exception as exc:
            self.call_from_thread(self._log, f"Bandcamp purchase check failed: {exc}")
            return
        if purchase:
            self._queue_store.update_status(item.queue_id, PENDING)
            self.call_from_thread(self._log, f"Purchase detected; queued item is pending: {item.hit.name}")
        else:
            self._queue_store.update_status(item.queue_id, NEEDS_PURCHASE, "purchase required")
            self.call_from_thread(self._log, f"Purchase not detected; item still needs purchase: {item.hit.name}")
        self.call_from_thread(self._render_queue)
        self.call_from_thread(self._refresh_queue_indicators_for_item, item)

    def _download_queue(self) -> None:
        pending = self._queue_store.pending()
        if not pending:
            self._log("Queue has no pending items.")
            return
        self.query_one("#queue-download-all", Button).disabled = True
        self.query_one("#queue-clear", Button).disabled = True
        self.run_worker(self._download_queue_worker, thread=True)

    def _clear_queue(self) -> None:
        if any(item.status == DOWNLOADING for item in self._queue_items):
            self._log("Queue is downloading; wait until it finishes before clearing.")
            return
        self._queue_store.clear()
        self._queue_items = []
        self._queue_row_by_id = {}
        self._refresh_queue_status_cache()
        self._stop_queue_animation_if_idle()
        self._render_queue()
        self._refresh_local_indicators()
        self._log("Queue cleared.")

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

    def _download_queue_worker(self) -> None:
        try:
            pending_items = self._queue_store.pending()
            total = len(pending_items)
            for index, item in enumerate(pending_items, start=1):
                label = f"{item.hit.artist} - {item.hit.name}" if item.hit.artist else item.hit.name
                self.call_from_thread(self._log, f"Starting queued download {index}/{total}: {label}")
                self._queue_store.update_status(item.queue_id, DOWNLOADING)
                self.call_from_thread(self._apply_queue_item_status, item, DOWNLOADING)
                self.call_from_thread(self._start_queue_animation)

                try:
                    files = self._download_queue_item(item)
                except BandcampDownloadError as exc:
                    if str(exc) == "purchase required":
                        self._queue_store.update_status(item.queue_id, NEEDS_PURCHASE, "purchase required")
                        self.call_from_thread(
                            self._log,
                            f"Bandcamp purchase required: {item.hit.artist} - {item.hit.name}",
                        )
                        self.call_from_thread(self._apply_queue_item_status, item, NEEDS_PURCHASE)
                    else:
                        self._queue_store.update_status(item.queue_id, FAILED, str(exc)[-500:])
                        self.call_from_thread(self._log, f"Queue download failed: {exc}")
                        self.call_from_thread(self._apply_queue_item_status, item, FAILED)
                except BeatportDownloadError as exc:
                    self._queue_store.update_status(item.queue_id, FAILED, str(exc)[-500:])
                    self.call_from_thread(self._log, f"Queue download failed: {exc}")
                    self.call_from_thread(self._apply_queue_item_status, item, FAILED)
                except Exception as exc:
                    self._queue_store.update_status(item.queue_id, FAILED, str(exc)[-500:])
                    self.call_from_thread(self._log, f"Unexpected queue download error: {exc}")
                    self.call_from_thread(self._apply_queue_item_status, item, FAILED)
                else:
                    for file_path in files:
                        self._library_index.record_download(file_path, item.hit, origin=item.origin)
                    self._queue_store.update_status(item.queue_id, COMPLETED)
                    self.call_from_thread(
                        self._log,
                        f"Completed queued download: {item.hit.artist} - {item.hit.name}",
                    )
                    self.call_from_thread(self._apply_queue_item_status, item, COMPLETED, True)
        finally:
            self.call_from_thread(self._stop_queue_animation_if_idle)
            self.call_from_thread(lambda: setattr(self.query_one("#queue-download-all", Button), "disabled", False))
            self.call_from_thread(lambda: setattr(self.query_one("#queue-clear", Button), "disabled", False))

    def _download_queue_item(self, item: QueueItem) -> tuple[Path, ...]:
        hit = item.hit
        if hit.source == "bandcamp":
            collection = load_collection()
            purchase, _match_type = collection.find_purchase_for_hit(hit)
            if not purchase:
                raise BandcampDownloadError("purchase required")
            result = download_purchase(purchase)
            return result.files
        if hit.source == "beatport" and hit.item_id is not None:
            result = download_beatport_track(hit.item_id)
            return result.files
        raise BandcampDownloadError(f"Download is not supported for {hit.source}")

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
        collection = load_collection()
        purchase, match_type = collection.find_purchase_for_hit(hit)

        if not purchase:
            if not hit.url:
                raise BandcampDownloadError("Bandcamp result has no purchase page URL")
            self.call_from_thread(self._log, f"Opening Bandcamp purchase page: {hit.url}")
            open_bandcamp_page_and_wait_for_close(hit.url)
            collection = load_collection()
            purchase, match_type = collection.find_purchase_for_hit(hit)

        if not purchase:
            return None
        if not purchase.redownload_url:
            raise BandcampDownloadError(
                f"Matched purchase by {match_type}, but Bandcamp did not provide a redownload URL"
            )

        return download_purchase(purchase)

    def _enable_download_button(self) -> None:
        self.query_one("#download-selected", Button).disabled = False

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

    def _render_queue(self) -> None:
        self._queue_items = self._queue_store.list()
        self._refresh_queue_status_cache()
        self._queue_row_by_id = {}
        table = self.query_one("#queue-table", DataTable)
        table.clear()
        for row_index, item in enumerate(self._queue_items):
            self._queue_row_by_id[item.queue_id] = row_index
            table.add_row(
                self._queue_marker(item.status),
                item.hit.source,
                item.hit.name,
                item.hit.artist,
            )
        self._stop_queue_animation_if_idle()

    def _refresh_queue_status_cache(self) -> None:
        self._queue_status_by_hit = {item.queue_id: item.status for item in self._queue_items}
        spotify_status: dict[str, str] = {}
        for item in self._queue_items:
            spotify_track_id = item.origin.spotify_track_id if item.origin else None
            if spotify_track_id:
                spotify_status[spotify_track_id] = item.status
        self._queue_status_by_spotify_id = spotify_status

    def _queue_marker(self, status: str | None) -> Text | str:
        if status == DOWNLOADING:
            frame = self.SPLASH_SPINNER_FRAMES[
                self._queue_animation_frame % len(self.SPLASH_SPINNER_FRAMES)
            ]
            return Text(frame, style="cyan")
        if status == COMPLETED:
            return Text("✓", style="green")
        if status == FAILED:
            return Text("!", style="red")
        if status == NEEDS_PURCHASE:
            return Text("$", style="yellow")
        if status == PENDING:
            return "○"
        return ""

    def _apply_queue_item_status(
        self, item: QueueItem, status: str, force_spotify: bool = False
    ) -> None:
        updated_item = replace(item, status=status)
        self._queue_items = [
            updated_item if existing.queue_id == item.queue_id else existing
            for existing in self._queue_items
        ]
        self._refresh_queue_status_cache()

        row = self._queue_row_by_id.get(item.queue_id)
        queue_table = self.query_one("#queue-table", DataTable)
        if row is not None and row < queue_table.row_count:
            queue_table.update_cell_at(Coordinate(row, 0), self._queue_marker(status))
        else:
            self._render_queue()

        self._update_search_marker_for_queue_item(updated_item)
        self._update_spotify_marker_for_queue_item(updated_item, force_spotify)
        self._stop_queue_animation_if_idle()

    def _update_search_marker_for_queue_item(self, item: QueueItem) -> None:
        row = self._search_row_by_queue_id.get(item.queue_id)
        if row is None:
            return
        table = self.query_one("#results", DataTable)
        if row < table.row_count:
            table.update_cell_at(Coordinate(row, 1), self._marker_for_hit(item.hit))

    def _update_spotify_marker_for_queue_item(
        self, item: QueueItem, force_spotify: bool = False
    ) -> None:
        spotify_track_id = item.origin.spotify_track_id if item.origin else None
        if spotify_track_id:
            row = self._spotify_row_by_track_id.get(spotify_track_id)
            if row is not None:
                table = self.query_one("#spotify-browser", DataTable)
                if row < table.row_count:
                    marker = self._queue_marker(item.status)
                    if item.status == COMPLETED:
                        marker = self._marker_for_spotify_track(self._visible_spotify_tracks[row])
                    table.update_cell_at(Coordinate(row, 1), marker)
                    return
        if force_spotify or self._queue_item_affects_visible_spotify(item):
            self._refresh_spotify_indicators()

    def _start_queue_animation(self) -> None:
        if self._queue_animation_timer is None:
            self._queue_animation_timer = self.set_interval(0.12, self._tick_queue_animation)

    def _tick_queue_animation(self) -> None:
        self._queue_animation_frame += 1
        self._tick_queue_table_animation()
        self._tick_search_marker_animation()
        self._tick_spotify_marker_animation()

    def _tick_queue_table_animation(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        for item in self._queue_items:
            if item.status != DOWNLOADING:
                continue
            row = self._queue_row_by_id.get(item.queue_id)
            if row is None or row >= table.row_count:
                continue
            table.update_cell_at(Coordinate(row, 0), self._queue_marker(DOWNLOADING))

    def _tick_search_marker_animation(self) -> None:
        if not self._hits:
            return
        table = self.query_one("#results", DataTable)
        for queue_id, status in self._queue_status_by_hit.items():
            if status != DOWNLOADING:
                continue
            row = self._search_row_by_queue_id.get(queue_id)
            if row is None or row >= table.row_count:
                continue
            table.update_cell_at(Coordinate(row, 1), self._queue_marker(DOWNLOADING))

    def _tick_spotify_marker_animation(self) -> None:
        if self._spotify_mode != "tracks" or not self._visible_spotify_tracks:
            return
        table = self.query_one("#spotify-browser", DataTable)
        for spotify_track_id, status in self._queue_status_by_spotify_id.items():
            if status != DOWNLOADING:
                continue
            row = self._spotify_row_by_track_id.get(spotify_track_id)
            if row is None or row >= table.row_count:
                continue
            table.update_cell_at(Coordinate(row, 1), self._queue_marker(DOWNLOADING))

    def _stop_queue_animation_if_idle(self) -> None:
        if any(item.status == DOWNLOADING for item in self._queue_items):
            return
        if self._queue_animation_timer is not None:
            self._queue_animation_timer.stop()
            self._queue_animation_timer = None

    def _render_results(self, hits: list[BandcampSearchHit]) -> None:
        self._hits = hits
        self._search_row_by_queue_id = {}
        table = self.query_one("#results", DataTable)
        table.clear()
        for row_index, hit in enumerate(hits):
            self._search_row_by_queue_id[queue_id_for_hit(hit)] = row_index
            marker = self._marker_for_hit(hit)
            table.add_row(
                str(hit.rank),
                marker,
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
        self._spotify_row_by_track_id = {}
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
        for row_index, track in enumerate(self._visible_spotify_tracks):
            self._spotify_row_by_track_id[track.track_id] = row_index
            marker = self._marker_for_spotify_track(track)
            table.add_row(
                str(row_index + 1),
                marker,
                track.name,
                track.artist_label,
                track.album,
            )

    def _refresh_local_indicators(self) -> None:
        self._refresh_search_indicators()
        self._refresh_spotify_indicators()

    def _refresh_search_indicators(self) -> None:
        if self._hits:
            self._render_results(self._hits)

    def _refresh_spotify_indicators(self) -> None:
        if self._spotify_mode == "tracks" and self._spotify_tracks:
            self._render_spotify_tracks_preserving_view()

    def _refresh_queue_indicators_for_item(
        self, item: QueueItem, force_spotify: bool = False
    ) -> None:
        self._refresh_search_indicators()
        if force_spotify or self._queue_item_affects_visible_spotify(item):
            self._refresh_spotify_indicators()

    def _queue_item_affects_visible_spotify(self, item: QueueItem) -> bool:
        spotify_track_id = item.origin.spotify_track_id if item.origin else None
        if self._spotify_mode != "tracks":
            return False
        if spotify_track_id and any(
            track.track_id == spotify_track_id for track in self._visible_spotify_tracks
        ):
            return True
        hit_key = make_artist_title_key(item.hit.artist, item.hit.name)
        if not hit_key:
            return False
        for track in self._visible_spotify_tracks:
            artist = track.artists[0] if track.artists else ""
            if make_artist_title_key(artist, track.name) == hit_key:
                return True
        return False

    def _render_spotify_tracks_preserving_view(self) -> None:
        table = self.query_one("#spotify-browser", DataTable)
        cursor_row = table.cursor_row
        scroll_x = table.scroll_x
        scroll_y = table.scroll_y
        self._render_spotify_tracks(self._spotify_tracks)
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1), animate=False, scroll=False)
        table.set_scroll(scroll_x, scroll_y)

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

    def _marker_for_hit(self, hit: BandcampSearchHit) -> Text | str:
        status = self._queue_status_by_hit.get(queue_id_for_hit(hit))
        if status in {PENDING, NEEDS_PURCHASE, DOWNLOADING, FAILED}:
            return self._queue_marker(status)
        if status == COMPLETED:
            return self._queue_marker(COMPLETED)
        return self._local_marker(self._library_index.match_hit(hit).label)

    def _marker_for_spotify_track(self, track: SpotifyTrack) -> Text | str:
        status = self._queue_status_by_spotify_id.get(track.track_id)
        if status in {PENDING, NEEDS_PURCHASE, DOWNLOADING, FAILED}:
            return self._queue_marker(status)
        if status == COMPLETED:
            return self._queue_marker(COMPLETED)
        return self._local_marker(self._library_index.match_spotify_track(track).label)

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


def format_time(seconds: float | None) -> str:
    total = max(0, int(seconds or 0))
    minutes, remainder = divmod(total, 60)
    return f"{minutes}:{remainder:02d}"
