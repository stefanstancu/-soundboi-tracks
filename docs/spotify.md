# Spotify Integration

Spotify is used as a metadata browser, not a download backend. The TUI can browse your playlists, show tracks, and search a selected Spotify track across Bandcamp and Beatport.

## Spotify App Setup

1. Create an app at <https://developer.spotify.com/dashboard>.
2. Add this redirect URI:

```text
http://127.0.0.1:8765/callback
```

3. Provide the app client id to soundboi-tracks with either an environment variable:

```bash
export SPOTIFY_CLIENT_ID="your-client-id"
```

or a local config file:

```json
{
  "client_id": "your-client-id"
}
```

at `~/.config/soundboi-tracks/spotify.json`.

## TUI Flow

1. Run `soundboi-tracks tui`.
2. If no Spotify token is saved, approve the browser auth flow that opens automatically.
3. Playlists load automatically after startup/login.
4. Click a playlist to load its tracks.
5. Click a track to search Bandcamp and Beatport.

The selected track is searched across Bandcamp and Beatport. Downloads still happen through those provider backends.

Spotify currently allows playlist item loading only for playlists you own or collaborate on. The TUI marks playlists with an `Access` column and sorts accessible playlists first. Followed/public playlists owned by other users may appear in your playlist list but return `403 Forbidden` when loading tracks.

## Local Files

- Config: `~/.config/soundboi-tracks/spotify.json`
- Tokens: `~/.config/soundboi-tracks/spotify.tokens.json`

The token file is sensitive account access material. Do not commit it or share it.
