from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path


class PreviewPlayerError(RuntimeError):
    pass


class PreviewPlayer:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.ipc_path: Path | None = None

    def start(self, url: str) -> None:
        self.stop()
        command = self._player_command(url)
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if self.ipc_path:
            self._wait_for_ipc()

    def stop(self) -> None:
        if not self.process:
            self._cleanup_ipc()
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None
        self._cleanup_ipc()

    def is_playing(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def position(self) -> float | None:
        value = self._mpv_command(["get_property", "time-pos"])
        return float(value) if isinstance(value, int | float) else None

    def duration(self) -> float | None:
        value = self._mpv_command(["get_property", "duration"])
        return float(value) if isinstance(value, int | float) else None

    def seek(self, seconds: float) -> None:
        self._mpv_command(["seek", max(0, seconds), "absolute"])

    def _player_command(self, url: str) -> list[str]:
        mpv = shutil.which("mpv")
        if mpv:
            self.ipc_path = Path(tempfile.gettempdir()) / f"soundboi-preview-{os.getpid()}.sock"
            self._cleanup_ipc()
            return [
                mpv,
                "--no-video",
                "--really-quiet",
                f"--input-ipc-server={self.ipc_path}",
                url,
            ]
        ffplay = shutil.which("ffplay")
        if ffplay:
            self.ipc_path = None
            return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", url]
        raise PreviewPlayerError("Install mpv or ffplay to play previews")

    def _mpv_command(self, command: list[object]) -> object | None:
        if not self.ipc_path or not self.ipc_path.exists() or not self.is_playing():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(str(self.ipc_path))
                sock.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
                response = b""
                while not response.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            data = json.loads(response.decode("utf-8"))
        except Exception:
            return None
        if data.get("error") != "success":
            return None
        return data.get("data")

    def _wait_for_ipc(self) -> None:
        if not self.ipc_path:
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.ipc_path.exists():
                return
            time.sleep(0.05)

    def _cleanup_ipc(self) -> None:
        if self.ipc_path and self.ipc_path.exists():
            try:
                self.ipc_path.unlink()
            except OSError:
                pass
