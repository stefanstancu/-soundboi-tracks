from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widgets import Static


class PreviewScrubber(Static):
    class SeekRequested(Message):
        def __init__(self, seconds: float) -> None:
            super().__init__()
            self.seconds = seconds

    DEFAULT_CSS = """
    PreviewScrubber {
        height: 1;
    }
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__("", *args, **kwargs)
        self.position = 0.0
        self.duration = 0.0
        self._dragging = False

    def set_progress(self, position: float, duration: float) -> None:
        self.position = max(0.0, position)
        self.duration = max(0.0, duration)
        self.update(self._render_bar())

    def reset(self) -> None:
        self.position = 0.0
        self.duration = 0.0
        self.update(self._render_bar())

    def on_mount(self) -> None:
        self.reset()

    def on_resize(self) -> None:
        self.update(self._render_bar())

    def on_mouse_down(self, event) -> None:  # noqa: ANN001
        self._dragging = True
        self.capture_mouse()
        self._seek_from_x(float(event.x))
        event.stop()

    def on_mouse_move(self, event) -> None:  # noqa: ANN001
        if not self._dragging:
            return
        self._seek_from_x(float(event.x))
        event.stop()

    def on_mouse_up(self, event) -> None:  # noqa: ANN001
        if self._dragging:
            self._seek_from_x(float(event.x))
        self._dragging = False
        self.release_mouse()
        event.stop()

    def _seek_from_x(self, x: float) -> None:
        if self.duration <= 0:
            return
        width = max(1, self.size.width)
        fraction = min(1.0, max(0.0, x / max(1, width - 1)))
        seconds = fraction * self.duration
        self.position = seconds
        self.update(self._render_bar())
        self.post_message(self.SeekRequested(seconds))

    def _render_bar(self) -> Text:
        width = max(10, self.size.width or 32)
        if self.duration <= 0:
            return Text("─" * width, style="dim")

        fraction = min(1.0, max(0.0, self.position / self.duration))
        knob = min(width - 1, max(0, round((width - 1) * fraction)))
        text = Text()
        for index in range(width):
            if index == knob:
                text.append("●", style="bold cyan")
            elif index < knob:
                text.append("━", style="cyan")
            else:
                text.append("─", style="dim")
        return text
