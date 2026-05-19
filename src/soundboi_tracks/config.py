from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "soundboi-tracks"


def app_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def bandcamp_browser_dir() -> Path:
    return app_config_dir() / "bandcamp-browser"


def bandcamp_cookie_file() -> Path:
    return app_config_dir() / "bandcamp.cookies"


def bandcamp_download_dir() -> Path:
    base = os.environ.get("SOUNDBOI_DOWNLOAD_DIR")
    if base:
        return Path(base).expanduser() / "Bandcamp"
    return Path.home() / "Music" / APP_NAME / "Bandcamp"


def beatport_download_dir() -> Path:
    base = os.environ.get("SOUNDBOI_DOWNLOAD_DIR")
    if base:
        return Path(base).expanduser() / "Beatport"
    return Path.home() / "Music" / APP_NAME / "Beatport"


def orpheusdl_dir() -> Path:
    return Path(os.environ.get("ORPHEUSDL_DIR", "~/Music/OrpheusDL")).expanduser()


def beatport_token_file() -> Path:
    return app_config_dir() / "beatport.tokens.json"


def spotify_token_file() -> Path:
    return app_config_dir() / "spotify.tokens.json"


def spotify_config_file() -> Path:
    return app_config_dir() / "spotify.json"


def ensure_config_dir() -> Path:
    path = app_config_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()
