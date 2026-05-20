# soundboi-tracks

A small local tool for finding tracks across music providers, then handing the selected result to the right download backend.

## Initial Direction

- Keep this repo separate from `~/Music/OrpheusDL`.
- Treat OrpheusDL as a dependency/backend for providers it already supports, starting with Beatport.
- Add provider adapters for Bandcamp, Beatport, and other sources behind a common interface.
- Use Spotify as a playlist/track browser that can feed selected tracks into provider search.
- Start with proof commands before building the full TUI.

## Planned Proofs

1. Search Bandcamp and Beatport results for an artist/title query.
2. Authenticate Bandcamp through a browser-login cookie flow.
3. Match a Bandcamp result against purchased collection items.
4. Download a purchased item in MP3 320 where available.
5. Call OrpheusDL for Beatport search/download without modifying the OrpheusDL checkout.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
pip install -e ".[orpheus]"
python -m playwright install chromium
```

Secrets and cookies should live under `config/` or the OS keychain and are ignored by git.

## Run The TUI

```bash
source .venv/bin/activate
soundboi-tracks tui
```

The TUI has a thin provider bar showing Spotify, Bandcamp, and Beatport connection status. Login buttons appear in that bar only when a provider needs manual auth. Bandcamp auth opens a dedicated browser profile, waits for you to log in manually, captures the resulting session cookies, and stores them at `~/.config/soundboi-tracks/bandcamp.cookies`.

The TUI includes combined search. Enter an artist/title query and press `Search` to query Bandcamp and Beatport concurrently, then combine the results.

Purchase/download flow:

1. Search for a track.
2. Select a result.
3. Press `Download Selected`.
4. For Bandcamp, the app checks your collection. If you do not own the result yet, it opens the Bandcamp page for manual purchase.
5. Close the Bandcamp browser after purchase. The app checks ownership again and downloads automatically if the purchase is detected.

Downloads are consolidated under `~/Music/soundboi-tracks/downloads/` regardless of provider. Bandcamp and Beatport source metadata is tracked in a local SQLite index at `~/.config/soundboi-tracks/library-index.sqlite3`.

The TUI indexes the local library in the background. Search results and Spotify playlist tracks are highlighted when they appear to already exist locally: `✓` means an exact provider/Spotify match, and `~` means a likely artist/title match. This is only an indicator; it does not prevent re-downloading.

Search results can also be added to a persistent queue stored in the SQLite index. Queue states use compact markers: `○` pending, animated spinner while downloading, `✓` completed, and `!` failed. `Download All` skips unpurchased Bandcamp items for now and leaves them pending.

Beatport downloads run OrpheusDL with this project's active Python environment, so install the `orpheus` extra before using Beatport download support.

## Spotify

Spotify playlist browsing requires a Spotify developer app client id. Add `http://127.0.0.1:8765/callback` as the app redirect URI, then set `SPOTIFY_CLIENT_ID` or write `~/.config/soundboi-tracks/spotify.json` with a `client_id`. The TUI loads playlists automatically when already authenticated, shows a provider-bar login button when needed, loads tracks when you click a playlist, and searches the selected track when you click it. See `docs/spotify.md`.

You can smoke-test search without the TUI:

```bash
soundboi-tracks search burial archangel
```
