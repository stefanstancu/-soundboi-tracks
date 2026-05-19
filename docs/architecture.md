# Architecture Notes

## Provider Interface

Each provider should expose the same high-level capabilities:

- `search(query)`: return normalized search hits.
- `auth_status()`: report whether the provider can download owned items.
- `download(hit, output_dir, quality)`: download through the provider's allowed backend.

## Backends

- Beatport: search directly against the Beatport catalog API and call into the existing OrpheusDL checkout for downloads.
- Bandcamp: use public search for discovery and authenticated collection/redownload URLs for owned downloads.
- Spotify: browse playlists and tracks as a metadata source, then search selected tracks on acquisition backends.
- SoundCloud: defer until Bandcamp, Beatport, and Spotify proofs are working.

## Search

Search runs provider queries concurrently and combines results after all providers return. Bandcamp uses the public fuzzy autocomplete endpoint. Beatport uses the catalog search API with local credentials/tokens from the existing OrpheusDL setup.

Spotify is intentionally not part of the acquisition search result set. It populates a left-side playlist browser and can submit the selected track's artist/title query to the combined Bandcamp/Beatport search pane.

Bandcamp ownership is checked lazily when downloading. The downloader loads the user's purchased collection and matches by exact item id, album id, artist/title, and album tracklists.

## Purchase And Download

Purchases stay manual: when a selected Bandcamp result is not already owned, the TUI opens the Bandcamp page and waits for the user to close the browser. After the page closes, the collection is refreshed and the result is downloaded automatically if ownership is detected. The Bandcamp downloader prefers `mp3-320`, then falls back through common lossless/high-quality formats. Beatport selected results are passed to OrpheusDL for download.

## Local Library Index

Downloaded audio is consolidated under `~/Music/soundboi-tracks/downloads/` independent of provider. A SQLite index in `~/.config/soundboi-tracks/library-index.sqlite3` stores provider ids, normalized artist/title keys, optional Spotify origin ids, and file stats. Startup loads the index immediately and refreshes it in the background with cheap path/mtime/size checks. Search and Spotify rows use the in-memory lookup maps to show exact or likely local matches without blocking re-downloads.

## Auth

Bandcamp auth should use a browser-login flow that captures session cookies after the user signs in manually. The tool should not store account passwords.

The first implementation uses Playwright with a persistent Chromium profile in `~/.config/soundboi-tracks/bandcamp-browser`. Once the login cookies are present, the auth backend verifies them against the Bandcamp homepage and writes a cookie header to `~/.config/soundboi-tracks/bandcamp.cookies` with private file permissions.
