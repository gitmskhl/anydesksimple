"""Асинхронная P2P-сессия в отдельном потоке, без вызовов Tk из этого потока."""
import asyncio
from contextlib import suppress
import json
import queue
import ssl
import threading
import time

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

import config
from desktop import Mouse
from media import MicrophoneTrack, ScreenTrack, Speaker, put_latest
from protocol import read_packet, write_packet


class SessionEnded(Exception):
    pass


class Session(threading.Thread):
    def __init__(self, role, code="", name="Оператор"):
        super().__init__(daemon=True)
        self.role, self.code, self.name = role, code, name
        self.events = queue.Queue()
        self.frames = queue.Queue(maxsize=1)
        self.loop = None
        self.writer = None
        self.pc = None
        self.channel = None
        self.screen = None
        self.microphone = None
        self.speaker = None
        self.mouse = None
        self.mouse_allowed = False
        self.mouse_request_pending = False
        self.last_mouse_request = 0.0
        self.stopping = threading.Event()
        self.tasks = set()
        self.connected = None
        self.cleaning = False
        self.main_task = None
        self.permission_generation = 0
        self.approved = False

    def emit(self, kind, value=None):
        self.events.put((kind, value))

    def run(self):
        asyncio.run(self._run())

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.main_task = asyncio.current_task()
        self.connected = asyncio.Event()
        try:
            if self.stopping.is_set():
                return
            context = ssl.create_default_context(cafile=str(config.SIGNALING_CERT))
            # Проверяется доверенный сертификат, скопированный с сервера.
            # Имя отключено, чтобы пользователь мог менять IP без перевыпуска сертификата.
            context.check_hostname = False
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.emit("status", "Соединение с signaling-сервером…")
            reader, self.writer = await asyncio.wait_for(asyncio.open_connection(
                config.SIGNALING_IP, config.SIGNALING_PORT, ssl=context,
                server_hostname="MiniDesk signaling"), 15)
            if self.stopping.is_set():
                return
            if self.role == "client":
                await self.signal({"type": "register"})
            else:
                await self.signal({"type": "join", "code": self.code, "name": self.name})
            while not self.stopping.is_set():
                await self.on_signal(await read_packet(reader))
        except SessionEnded as exc:
            self.emit("status", str(exc))
        except asyncio.CancelledError:
            pass
        except asyncio.IncompleteReadError:
            if not self.stopping.is_set():
                self.emit("status", "Соединение завершено")
        except Exception as exc:
            if not self.stopping.is_set():
                self.emit("error", f"{type(exc).__name__}: {exc}")
        finally:
            await self.cleanup()
            self.emit("closed")

    async def signal(self, message):
        await write_packet(self.writer, message)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def done(finished):
            self.tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error and not self.stopping.is_set():
                self.emit("error", str(error))
                self.stop()
        task.add_done_callback(done)
        return task

    async def ask(self, text):
        future = self.loop.create_future()
        self.emit("prompt", (future, text))
        try:
            return await asyncio.wait_for(future, config.CONSENT_TIMEOUT)
        except TimeoutError:
            return False

    def answer_prompt(self, future, allow):
        def resolve():
            if not future.done():
                future.set_result(allow)
        self.post(resolve)

    def post(self, callback, *args):
        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:
                pass

    async def on_signal(self, message):
        kind = message["type"]
        if kind == "registered":
            self.emit("code", message["code"])
            self.emit("status", "Ожидание оператора. Передайте ему код подключения.")
        elif kind == "waiting":
            self.emit("status", "Ожидание согласия клиента…")
        elif kind == "request" and self.role == "client" and not self.approved and self.pc is None:
            self.emit("status", "Входящий запрос подключения")
            allow = await self.ask(
                f"Оператор «{message['name']}» просит подключиться.\n\n"
                "Разрешить показ вашего экрана и воспроизведение голоса оператора?\n"
                "Управление мышью потребует отдельного согласия.\n\n"
                "Имя оператора указано им самим. Проверяйте, кому передаёте код.")
            if self.stopping.is_set():
                return
            self.approved = allow
            await self.signal({"type": "decision", "allow": allow})
            self.emit("status", "Подключение P2P…" if allow else "Подключение отклонено")
            if allow:
                self.spawn(self.watch_connection())
        elif kind == "accepted" and self.role == "control":
            self.emit("status", "Клиент согласился. Поиск прямого P2P-маршрута…")
            self.create_pc()
            self.microphone = MicrophoneTrack()  # Устройство пока НЕ открывается.
            self.pc.addTransceiver(self.microphone, direction="sendonly")
            self.pc.addTransceiver("video", direction="recvonly")
            self.attach_channel(self.pc.createDataChannel("mouse", ordered=True))
            self.spawn(self.watch_connection())
            await self.pc.setLocalDescription(await self.pc.createOffer())
            await self.signal({"type": "offer", "sdp": self.pc.localDescription.sdp})
        elif kind == "offer" and self.role == "client" and self.approved and self.pc is None:
            self.create_pc()
            await self.pc.setRemoteDescription(RTCSessionDescription(message["sdp"], "offer"))
            self.screen = ScreenTrack()
            self.pc.addTrack(self.screen)
            self.mouse = Mouse()
            await self.pc.setLocalDescription(await self.pc.createAnswer())
            await self.signal({"type": "answer", "sdp": self.pc.localDescription.sdp})
        elif kind == "answer" and self.role == "control" and self.pc:
            await self.pc.setRemoteDescription(RTCSessionDescription(message["sdp"], "answer"))
        elif kind == "rejected":
            raise SessionEnded("Клиент отклонил запрос или не ответил вовремя")
        elif kind == "peer_left":
            raise SessionEnded("Другой участник отключился. Для новой сессии нужен новый код.")
        elif kind == "error":
            raise RuntimeError(message.get("message", "Ошибка signaling-сервера"))
        else:
            raise ValueError("Неожиданное сообщение signaling-сервера")

    def create_pc(self):
        self.pc = RTCPeerConnection(RTCConfiguration(
            iceServers=[RTCIceServer(urls=url) for url in config.STUN_URLS]))

        @self.pc.on("connectionstatechange")
        async def state_changed():
            state = self.pc.connectionState
            if state == "connected":
                self.connected.set()
                self.emit("connected")
                self.emit("status", "P2P подключено. Экран передаётся; микрофон оператора выключен.")
            elif state == "failed" and not self.stopping.is_set():
                self.emit("error", "P2P-соединение потеряно или прямой маршрут недоступен")
                self.stop()

        @self.pc.on("datachannel")
        def data_channel(channel):
            if self.role == "client" and channel.label == "mouse" and self.channel is None:
                self.attach_channel(channel)
            else:
                channel.close()

        @self.pc.on("track")
        def track_received(track):
            if track.kind == "video" and self.role == "control":
                self.spawn(self.receive_video(track))
            elif track.kind == "audio" and self.role == "client":
                self.spawn(self.receive_audio(track))

    async def watch_connection(self):
        try:
            await asyncio.wait_for(self.connected.wait(), config.CONNECT_TIMEOUT)
        except TimeoutError:
            raise RuntimeError("Не удалось установить прямой P2P-канал. Проверьте UDP/firewall. "
                               "При CGNAT/симметричном NAT может потребоваться VPN или TURN.")
        while not self.stopping.is_set():
            if self.screen and self.screen.error:
                raise RuntimeError(f"Захват экрана: {self.screen.error}")
            await asyncio.sleep(1)

    def attach_channel(self, channel):
        self.channel = channel

        @channel.on("open")
        def opened():
            self.emit("channel_ready")

        @channel.on("close")
        def closed():
            if self.mouse:
                self.mouse.release()
            self.mouse_allowed = False
            self.emit("mouse", False)
            if not self.stopping.is_set():
                self.stop()

        @channel.on("message")
        def received(raw):
            if not isinstance(raw, str) or len(raw) > 1024:
                return
            try:
                message = json.loads(raw)
                if isinstance(message, dict):
                    self.on_command(message)
            except (ValueError, TypeError):
                return

    def send_command(self, message):
        if self.channel and self.channel.readyState == "open":
            if message.get("type") == "move" and self.channel.bufferedAmount >= 65536:
                return False
            if self.channel.bufferedAmount >= 262144:
                # При перегрузке закрытие канала отпускает кнопки на клиенте.
                self.emit("error", "Канал команд перегружен; соединение остановлено")
                self.stop()
                return False
            self.channel.send(json.dumps(message, allow_nan=False))
            return True
        return False

    def on_command(self, message):
        kind = message.get("type")
        if self.role == "control":
            if kind == "mouse_permission":
                self.mouse_allowed = message.get("allow") is True
                self.mouse_request_pending = False
                self.emit("mouse", self.mouse_allowed)
            return
        if kind == "request_mouse":
            if (not self.mouse_request_pending and not self.mouse_allowed
                    and time.monotonic() - self.last_mouse_request >= 3):
                self.mouse_request_pending = True
                self.last_mouse_request = time.monotonic()
                self.spawn(self.decide_mouse())
            elif not self.mouse_request_pending:
                # Повторный быстрый запрос тоже получает ответ, иначе кнопка
                # оператора навсегда осталась бы в состоянии «Ожидание…».
                self.send_command({"type": "mouse_permission", "allow": self.mouse_allowed})
        elif kind == "release_mouse":
            self.revoke_mouse()
        elif kind == "microphone":
            self.emit("remote_mic", message.get("enabled") is True)
        elif self.mouse_allowed and self.mouse:
            try:
                self.mouse.apply(message, self.screen.monitor if self.screen else None)
            except Exception as exc:
                self.revoke_mouse()
                self.emit("notice", f"Управление мышью остановлено: {exc}")

    async def decide_mouse(self):
        generation = self.permission_generation
        try:
            allow = await self.ask("Разрешить оператору управлять вашей мышью?\n\n"
                                   "Он сможет перемещать указатель, нажимать кнопки, "
                                   "перетаскивать и прокручивать.\n"
                                   "Можно отозвать доступ кнопкой в этом окне.")
            if (self.stopping.is_set() or not self.channel or self.channel.readyState != "open"
                    or generation != self.permission_generation):
                return
            self.mouse_allowed = allow
            self.send_command({"type": "mouse_permission", "allow": allow})
            self.emit("mouse", allow)
        finally:
            self.mouse_request_pending = False

    def revoke_mouse(self):
        self.permission_generation += 1
        self.mouse_allowed = False
        if self.mouse:
            self.mouse.release()
        self.send_command({"type": "mouse_permission", "allow": False})
        self.emit("mouse", False)

    def toggle_mouse(self):
        if self.mouse_allowed:
            self.mouse_allowed = False
            self.send_command({"type": "release_mouse"})
            self.emit("mouse", False)
        elif not self.mouse_request_pending:
            if self.send_command({"type": "request_mouse"}):
                self.mouse_request_pending = True
                self.emit("mouse_pending")

    def toggle_microphone(self):
        if not self.microphone or not self.connected.is_set():
            return
        try:
            enabled = self.microphone.stream is None
            self.microphone.enable(enabled)
            self.send_command({"type": "microphone", "enabled": enabled})
            self.emit("mic", enabled)
        except Exception as exc:
            self.emit("mic", False)
            self.emit("notice", f"Не удалось включить микрофон: {exc}")

    async def receive_video(self, track):
        try:
            while True:
                frame = await track.recv()
                put_latest(self.frames, frame.to_image())
        except MediaStreamError:
            if not self.stopping.is_set():
                raise RuntimeError("Поток экрана завершился")

    async def receive_audio(self, track):
        try:
            self.speaker = Speaker()
        except Exception as exc:
            self.emit("notice", f"Динамики недоступны: {exc}. Экран продолжит работать.")
        try:
            while True:
                frame = await track.recv()
                if self.speaker:
                    self.speaker.push(frame)
        except MediaStreamError:
            pass

    def stop(self):
        self.stopping.set()
        self.post(self._disconnect)

    def _disconnect(self):
        if self.writer:
            self.writer.close()
        if self.main_task and not self.main_task.done() and not self.cleaning:
            self.main_task.cancel()

    async def cleanup(self):
        if self.cleaning:
            return
        self.cleaning = True
        self.stopping.set()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        # Сбой отключённого аудиоустройства не должен мешать закрытию P2P.
        for resource, method in [(self.mouse, "release"), (self.screen, "stop"),
                                 (self.microphone, "stop"), (self.speaker, "close")]:
            if resource:
                with suppress(Exception):
                    getattr(resource, method)()
        if self.pc:
            with suppress(Exception):
                await self.pc.close()
        if self.writer:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), 3)
            except (OSError, TimeoutError):
                pass
