"""Запускается у оператора: экран клиента, мышь и свой микрофон."""
from desktop import enable_dpi_awareness
enable_dpi_awareness()

import queue
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from peer import Session
from ui import Window


class ControlWindow(Window):
    def __init__(self, root):
        super().__init__(root)
        root.title("MiniDesk — оператор")
        root.geometry("1180x800")
        root.minsize(850, 560)
        self.mouse_allowed = False
        self.last_motion = 0
        self.image_rect = None
        self.last_image = None
        self.photo = None
        self.canvas_item = None
        self.render_size = None
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        connection = ttk.Frame(frame)
        connection.pack(fill="x")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Код клиента:").grid(row=0, column=0)
        self.code = ttk.Entry(connection, width=32)
        self.code.grid(row=0, column=1, padx=8, sticky="ew")
        ttk.Label(connection, text="Ваше имя:").grid(row=0, column=2)
        self.name = ttk.Entry(connection, width=12)
        self.name.insert(0, "Оператор")
        self.name.grid(row=0, column=3, padx=8)
        self.connect = ttk.Button(connection, text="Подключиться", command=self.start)
        self.connect.grid(row=0, column=4)
        self.disconnect = ttk.Button(connection, text="Отключить", command=self.stop, state="disabled")
        self.disconnect.grid(row=0, column=5, padx=(8, 0))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=10)
        self.mouse_button = ttk.Button(toolbar, text="Запросить управление мышью",
                                       command=lambda: self.post("toggle_mouse"), state="disabled")
        self.mouse_button.pack(side="left")
        self.mic_button = ttk.Button(toolbar, text="Включить микрофон",
                                     command=lambda: self.post("toggle_microphone"), state="disabled")
        self.mic_button.pack(side="left", padx=10)
        ttk.Label(toolbar, text="Esc — отпустить мышь; клавиатура не передаётся").pack(side="left", padx=6)
        self.canvas = tk.Canvas(frame, background="#171b22", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.placeholder = self.canvas.create_text(25, 25, anchor="nw", fill="#c4cbd5",
                                                   text="Экран появится после согласия клиента")
        ttk.Label(frame, textvariable=self.status, wraplength=1100).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, textvariable=self.notice, foreground="#a12626", wraplength=1100).pack(anchor="w")
        self.canvas.bind("<Motion>", self.motion)
        for number, button in [(1, "left"), (2, "middle"), (3, "right")]:
            self.canvas.bind(f"<ButtonPress-{number}>", lambda event, b=button: self.pointer(event, "down", b))
            self.canvas.bind(f"<ButtonRelease-{number}>", lambda event, b=button: self.pointer(event, "up", b))
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Button-4>", lambda event: self.pointer(event, "scroll", amount=1))
        self.canvas.bind("<Button-5>", lambda event: self.pointer(event, "scroll", amount=-1))
        self.canvas.bind("<FocusOut>", lambda _: self.release_buttons())
        root.bind("<FocusOut>", lambda _: self.release_buttons())
        root.bind("<Escape>", lambda _: self.release_mouse())

    def start(self):
        if self.worker:
            return
        code = self.code.get().strip()
        if ":" not in code or len(code) > 100:
            self.notice.set("Вставьте полный код из окна client.py, включая двоеточие.")
            return
        self.connect.configure(state="disabled")
        self.disconnect.configure(state="normal")
        self.code.configure(state="disabled")
        self.last_image = None
        self.image_rect = None
        self.start_worker(Session("control", code, self.name.get().strip()[:80] or "Оператор"))

    def on_event(self, kind, value):
        if kind == "connected":
            self.mic_button.configure(state="normal")
        elif kind == "channel_ready":
            self.mouse_button.configure(state="normal")
        elif kind == "mouse_pending":
            self.mouse_button.configure(text="Ожидание согласия…", state="disabled")
        elif kind == "mouse":
            self.mouse_allowed = value
            self.mouse_button.configure(text="Отпустить мышь" if value else "Запросить управление мышью",
                                        state="normal" if self.worker and not self.worker.stopping.is_set() else "disabled")
        elif kind == "mic":
            self.mic_button.configure(text="Выключить микрофон" if value else "Включить микрофон")
        elif kind == "closed":
            self.mouse_allowed = False
            self.image_rect = None
            self.last_image = None
            self.canvas.delete("all")
            self.canvas_item = None
            self.photo = None
            self.placeholder = self.canvas.create_text(25, 25, anchor="nw", fill="#c4cbd5",
                                                       text="Сеанс завершён")
            self.connect.configure(state="normal")
            self.disconnect.configure(state="disabled")
            self.code.configure(state="normal")
            self.mouse_button.configure(state="disabled", text="Запросить управление мышью")
            self.mic_button.configure(state="disabled", text="Включить микрофон")

    def on_frame(self, worker):
        fresh = False
        try:
            self.last_image = worker.frames.get_nowait()
            fresh = True
        except queue.Empty:
            pass
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if self.last_image is None or min(size) <= 1 or (not fresh and self.render_size == size):
            return
        self.render_size = size
        image = self.last_image
        scale = min(size[0] / image.width, size[1] / image.height)
        width, height = max(1, int(image.width * scale)), max(1, int(image.height * scale))
        x, y = (size[0] - width) // 2, (size[1] - height) // 2
        self.image_rect = (x, y, width, height)
        self.photo = ImageTk.PhotoImage(image.resize((width, height), Image.Resampling.BILINEAR))
        self.canvas.delete(self.placeholder)
        if self.canvas_item is None:
            self.canvas_item = self.canvas.create_image(x, y, anchor="nw", image=self.photo)
        else:
            self.canvas.coords(self.canvas_item, x, y)
            self.canvas.itemconfigure(self.canvas_item, image=self.photo)

    def motion(self, event):
        now = time.monotonic()
        if now - self.last_motion >= 1 / 30:
            self.last_motion = now
            self.pointer(event, "move")

    def pointer(self, event, kind, button=None, amount=None):
        if not self.mouse_allowed or not self.image_rect or not self.worker:
            return
        x, y, width, height = self.image_rect
        if kind != "up" and not (x <= event.x < x + width and y <= event.y < y + height):
            return
        # Отпускание вне изображения тоже доставляется: перетаскивание не залипнет.
        message = {"type": kind, "x": min(1, max(0, (event.x - x) / max(1, width - 1))),
                   "y": min(1, max(0, (event.y - y) / max(1, height - 1)))}
        if button:
            message["button"] = button
        if amount is not None:
            message["amount"] = amount
        if kind == "down":
            self.canvas.focus_set()
        self.post("send_command", message)

    def wheel(self, event):
        if event.delta:
            amount = (1 if event.delta > 0 else -1) * max(1, min(10, abs(event.delta) // 120))
            self.pointer(event, "scroll", amount=amount)

    def release_buttons(self):
        if self.mouse_allowed:
            self.post("send_command", {"type": "release_buttons"})

    def release_mouse(self):
        if self.mouse_allowed:
            self.post("toggle_mouse")


if __name__ == "__main__":
    root = tk.Tk()
    ControlWindow(root)
    root.mainloop()
