"""DPI и строго ограниченный набор команд мыши."""
import math
import sys


def enable_dpi_awareness():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


class Mouse:
    def __init__(self):
        import pyautogui
        self.api = pyautogui
        self.api.PAUSE = 0
        self.pressed = set()

    def apply(self, message, monitor):
        kind = message.get("type")
        if kind == "release_buttons":
            self.release()
            return
        if kind not in {"move", "down", "up", "scroll"} or not monitor:
            return
        x, y = message.get("x"), message.get("y")
        if (isinstance(x, bool) or isinstance(y, bool)
                or not isinstance(x, (float, int)) or not isinstance(y, (float, int))
                or not math.isfinite(x) or not math.isfinite(y)
                or not 0 <= x <= 1 or not 0 <= y <= 1):
            return
        button = message.get("button")
        if kind in {"down", "up"} and button not in {"left", "middle", "right"}:
            return
        self.api.moveTo(monitor["left"] + round(x * (monitor["width"] - 1)),
                        monitor["top"] + round(y * (monitor["height"] - 1)))
        if kind == "down":
            self.api.mouseDown(button=button)
            self.pressed.add(button)
        elif kind == "up":
            self.api.mouseUp(button=button)
            self.pressed.discard(button)
        elif kind == "scroll":
            amount = message.get("amount")
            if isinstance(amount, int) and not isinstance(amount, bool) and -10 <= amount <= 10:
                self.api.scroll(amount)

    def release(self):
        # Отпускаем нажатые удалённо кнопки даже при сработавшем corner failsafe.
        for button in tuple(self.pressed):
            enabled = self.api.FAILSAFE
            try:
                # Только отпускание: движение/нажатия всегда используют FAILSAFE.
                self.api.FAILSAFE = False
                self.api.mouseUp(button=button, _pause=False)
            finally:
                self.api.FAILSAFE = enabled
                self.pressed.discard(button)
