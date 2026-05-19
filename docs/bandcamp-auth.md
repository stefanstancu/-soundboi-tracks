# Bandcamp Auth Flow

The tool should never store a Bandcamp password. It uses a real browser session instead.

## Flow

1. Run `soundboi-tracks tui`.
2. Press `Login to Bandcamp`.
3. Complete Bandcamp login in the Chromium window.
4. The app detects the login cookies, verifies them against `https://bandcamp.com/`, and closes the browser profile.
5. The cookie header is saved to `~/.config/soundboi-tracks/bandcamp.cookies`.

The TUI logs cookie names only, not cookie values.

## Local Files

- Browser profile: `~/.config/soundboi-tracks/bandcamp-browser/`
- Cookie header: `~/.config/soundboi-tracks/bandcamp.cookies`

The cookie file is sensitive account access material. Do not commit it, share it, or copy it to other machines casually.

## Next Backend Steps

- Improve purchased collection caching so each search does not reload the full collection.
- Add format selection in the TUI.
- Add download progress reporting.
- Add a post-download import/sort handoff.
