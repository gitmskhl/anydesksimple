"""Общая часть Tk: UI-поток, очередь событий и немодальные окна согласия."""
import queue
import tkinter as tk
from tkinter import ttk

import config


class Window:
    def __init__(self, root):
        self.root = root
        self.worker = None
        self.dialogs = set()
        self.closing = False
        self.status = tk.StringVar(value="Готово")
        self.notice = tk.StringVar()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(30, self.poll)

    def start_worker(self, worker):
        self.worker = worker
        self.notice.set("")
        worker.start()

    def stop(self):
        if self.worker:
            self.status.set("Отключение…")
            self.worker.stop()
        for dialog in tuple(self.dialogs):
            dialog.destroy()
        self.dialogs.clear()

    def post(self, method, *args):
        if self.worker:
            worker = self.worker
            worker.post(getattr(worker, method), *args)

    def poll(self):
        worker = self.worker
        if worker:
            # Один видеокадр на проход; служебная очередь остаётся отзывчивой.
            for _ in range(50):
                try:
                    kind, value = worker.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "prompt":
                    self.prompt(worker, *value)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "error":
                    self.status.set("Ошибка подключения")
                    self.notice.set(value)
                elif kind == "notice":
                    self.notice.set(value)
                elif kind == "closed":
                    if self.status.get() == "Отключение…":
                        self.status.set("Сеанс завершён")
                    for dialog in tuple(self.dialogs):
                        dialog.destroy()
                    self.dialogs.clear()
                    self.on_event(kind, value)
                    self.worker = None
                    break
                else:
                    self.on_event(kind, value)
            if self.worker is worker:
                self.on_frame(worker)
        if not self.closing:
            self.root.after(30, self.poll)

    def prompt(self, worker, future, text):
        if worker.stopping.is_set():
            return
        dialog = tk.Toplevel(self.root)
        self.dialogs.add(dialog)
        dialog.title("Разрешение доступа")
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=text, wraplength=510, justify="left").pack(anchor="w")
        remaining = tk.StringVar()
        ttk.Label(frame, textvariable=remaining).pack(anchor="w", pady=(15, 8))

        def answer(allow):
            worker.answer_prompt(future, allow)
            self.dialogs.discard(dialog)
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Отклонить", command=lambda: answer(False)).pack(side="right")
        ttk.Button(buttons, text="Разрешить", command=lambda: answer(True)).pack(side="right", padx=8)
        dialog.protocol("WM_DELETE_WINDOW", lambda: answer(False))

        def tick(seconds):
            if not dialog.winfo_exists():
                return
            if seconds <= 0 or worker.stopping.is_set():
                answer(False)
                return
            remaining.set(f"Автоматический отказ через {seconds} сек.")
            dialog.after(1000, tick, seconds - 1)
        tick(config.CONSENT_TIMEOUT)
        dialog.lift()

    def on_event(self, kind, value):
        pass

    def on_frame(self, worker):
        pass

    def close(self):
        self.closing = True
        self.stop()
        self._wait_close()

    def _wait_close(self):
        if self.worker and self.worker.is_alive():
            self.root.after(50, self._wait_close)
        else:
            self.root.destroy()
