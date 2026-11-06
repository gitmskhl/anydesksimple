"""Запускается на компьютере, чей экран показывается оператору."""
from desktop import enable_dpi_awareness
enable_dpi_awareness()

import tkinter as tk
from tkinter import ttk

from peer import Session
from ui import Window


class ClientWindow(Window):
    def __init__(self, root):
        super().__init__(root)
        root.title("MiniDesk — клиент")
        root.geometry("680x530")
        root.minsize(580, 500)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Доступ к этому компьютеру", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Передавайте код только тому, кому доверяете.").pack(anchor="w", pady=(8, 15))
        self.code = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.code, state="readonly", font=("Consolas", 12))
        self.entry.pack(fill="x")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=12)
        self.start_button = ttk.Button(buttons, text="Получить код", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="Копировать код", command=self.copy_code).pack(side="left", padx=8)
        self.stop_button = ttk.Button(buttons, text="Отключить", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.mouse_text = tk.StringVar(value="Мышь: доступ запрещён")
        self.mic_text = tk.StringVar(value="Микрофон оператора: выключен")
        ttk.Label(frame, textvariable=self.mouse_text).pack(anchor="w", pady=(8, 5))
        ttk.Label(frame, textvariable=self.mic_text).pack(anchor="w")
        self.revoke = ttk.Button(frame, text="Запретить управление мышью",
                                 command=lambda: self.post("revoke_mouse"), state="disabled")
        self.revoke.pack(anchor="w", pady=12)
        top = tk.BooleanVar(value=True)
        root.attributes("-topmost", True)
        ttk.Checkbutton(frame, text="Поверх остальных окон", variable=top,
                        command=lambda: root.attributes("-topmost", top.get())).pack(anchor="w")
        ttk.Separator(frame).pack(fill="x", pady=12)
        ttk.Label(frame, textvariable=self.status, wraplength=620).pack(anchor="w")
        ttk.Label(frame, textvariable=self.notice, foreground="#a12626", wraplength=620).pack(anchor="w", pady=6)
        root.bind("<Escape>", lambda _: self.post("revoke_mouse"))

    def start(self):
        if self.worker:
            return
        self.code.set("")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.start_worker(Session("client"))

    def copy_code(self):
        if self.code.get():
            self.root.clipboard_clear()
            self.root.clipboard_append(self.code.get())

    def on_event(self, kind, value):
        if kind == "code":
            self.code.set(value)
        elif kind == "mouse":
            self.mouse_text.set("Мышь: ОПЕРАТОР УПРАВЛЯЕТ" if value else "Мышь: доступ запрещён")
            self.revoke.configure(state="normal" if value else "disabled")
        elif kind == "remote_mic":
            self.mic_text.set("Микрофон оператора: ВКЛЮЧЁН" if value else "Микрофон оператора: выключен")
        elif kind == "closed":
            self.code.set("")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.revoke.configure(state="disabled")
            self.mouse_text.set("Мышь: доступ запрещён")
            self.mic_text.set("Микрофон оператора: выключен")


if __name__ == "__main__":
    root = tk.Tk()
    ClientWindow(root)
    root.mainloop()
