"""Реальные локальные TLS/WebRTC-соединения, искусственные медиа/мышь."""
import asyncio
from contextlib import ExitStack
from fractions import Fraction
from pathlib import Path
import queue
import ssl
import tempfile
import unittest
from unittest.mock import patch

from aiortc import VideoStreamTrack
import av
from PIL import Image

import config
from media import MicrophoneTrack
from peer import Session
from protocol import read_packet, write_packet
from signaling_server import SignalingServer, init_certificate


class SyntheticScreen(VideoStreamTrack):
    instances = []

    def __init__(self):
        super().__init__()
        self.monitor = {"left": 0, "top": 0, "width": 640, "height": 360}
        self.error = None
        self.instances.append(self)

    async def recv(self):
        pts, base = await self.next_timestamp()
        frame = av.VideoFrame.from_image(Image.new("RGB", (640, 360), "#3377bb"))
        frame.pts, frame.time_base = pts, base
        return frame


class SyntheticMouse:
    instances = []

    def __init__(self):
        self.commands = []
        self.pressed = set()
        self.instances.append(self)

    def apply(self, message, monitor):
        self.commands.append(message)
        if message["type"] == "down":
            self.pressed.add(message["button"])
        elif message["type"] == "up":
            self.pressed.discard(message["button"])
        elif message["type"] == "release_buttons":
            self.release()

    def release(self):
        self.pressed.clear()


class AudioSink:
    instances = []

    def __init__(self):
        self.frames = 0
        self.nonzero = False
        self.closed = False
        self.instances.append(self)

    def push(self, frame):
        self.frames += 1
        self.nonzero |= any(bytes(frame.planes[0])[:frame.samples * 2])

    def close(self):
        self.closed = True


class FakeInput:
    """Даёт ненулевой PCM без доступа к физическому микрофону."""
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.task = None

    def start(self):
        async def produce():
            while True:
                self.callback(b"\x00\x10" * config.AUDIO_SAMPLES,
                              config.AUDIO_SAMPLES, None, None)
                await asyncio.sleep(0.02)
        self.task = asyncio.create_task(produce())

    def abort(self):
        if self.task:
            self.task.cancel()

    def close(self):
        self.abort()


async def until(predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(config, "SIGNALING_CERT", Path(self.temp) / "cert.pem"))
        self.stack.enter_context(patch.object(config, "SIGNALING_KEY", Path(self.temp) / "key.pem"))
        init_certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(config.SIGNALING_CERT, config.SIGNALING_KEY)
        self.app = SignalingServer()
        self.server = await asyncio.start_server(self.app.handle, "127.0.0.1", 0, ssl=context)
        port = self.server.sockets[0].getsockname()[1]
        self.stack.enter_context(patch.object(config, "SIGNALING_IP", "127.0.0.1"))
        self.stack.enter_context(patch.object(config, "SIGNALING_PORT", port))
        self.stack.enter_context(patch.object(config, "STUN_URLS", []))
        self.stack.enter_context(patch("peer.ScreenTrack", SyntheticScreen))
        self.stack.enter_context(patch("peer.Mouse", SyntheticMouse))
        self.stack.enter_context(patch("peer.Speaker", AudioSink))
        self.stack.enter_context(patch("media.sd.RawInputStream", FakeInput))
        SyntheticScreen.instances.clear()
        SyntheticMouse.instances.clear()
        AudioSink.instances.clear()
        self.sessions = []
        self.jobs = []
        self.events = {}
        self.writers = []

    async def asyncTearDown(self):
        for session in self.sessions:
            session.stop()
        if self.jobs:
            await asyncio.wait_for(asyncio.gather(*self.jobs), 5)
        for session in self.sessions:
            if session.is_alive():
                await asyncio.to_thread(session.join, 5)
                self.assertFalse(session.is_alive(), "Поток сессии не завершился")
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.app.connections):
            writer.close()
        await until(lambda: not self.app.connections, timeout=3)
        self.stack.close()

    def start(self, role, code="", threaded=False):
        session = Session(role, code)
        self.sessions.append(session)
        if threaded:
            session.start()
        else:
            self.jobs.append(asyncio.create_task(session._run()))
        self.events[session] = []
        return session

    async def event(self, session, kind):
        async with asyncio.timeout(15):
            while True:
                try:
                    found, value = session.events.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                self.events[session].append((found, value))
                if found == "error":
                    self.fail(value)
                if found == kind:
                    return value

    async def connect_raw(self):
        context = ssl.create_default_context(cafile=str(config.SIGNALING_CERT))
        context.check_hostname = False
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", config.SIGNALING_PORT, ssl=context)
        self.writers.append(writer)
        return reader, writer

    async def test_wrong_secret_does_not_destroy_room(self):
        host_reader, host_writer = await self.connect_raw()
        await write_packet(host_writer, {"type": "register"})
        code = (await read_packet(host_reader))["code"]
        reader, writer = await self.connect_raw()
        await write_packet(writer, {"type": "join", "code": code.split(":")[0] + ":wrong"})
        self.assertEqual((await read_packet(reader))["type"], "error")
        self.assertIn(code.split(":")[0], self.app.rooms)
        self.assertIsNone(self.app.rooms[code.split(":")[0]].guest)

    async def test_offer_without_consent_is_rejected(self):
        host_reader, host_writer = await self.connect_raw()
        await write_packet(host_writer, {"type": "register"})
        code = (await read_packet(host_reader))["code"]
        reader, writer = await self.connect_raw()
        await write_packet(writer, {"type": "join", "code": code})
        self.assertEqual((await read_packet(reader))["type"], "waiting")
        self.assertEqual((await read_packet(host_reader))["type"], "request")
        await write_packet(writer, {"type": "offer", "sdp": "not allowed"})
        self.assertEqual((await read_packet(reader))["type"], "error")

    async def test_client_independently_requires_local_consent(self):
        # Даже неверно работающий signaling не может сам разрешить захват.
        host = Session("client")
        with self.assertRaises(ValueError):
            await host.on_signal({"type": "offer", "sdp": "unsolicited"})
        self.assertIsNone(host.pc)
        self.assertFalse(SyntheticScreen.instances)

    async def test_denial_starts_no_media(self):
        host = self.start("client")
        code = await self.event(host, "code")
        operator = self.start("control", code)
        future, _ = await self.event(host, "prompt")
        self.assertIsNone(host.pc)
        self.assertFalse(SyntheticScreen.instances)
        host.answer_prompt(future, False)
        await self.event(operator, "closed")
        await self.event(host, "closed")
        self.assertFalse(SyntheticScreen.instances)
        self.assertIsNone(operator.microphone)

    async def test_stop_while_consent_is_pending(self):
        host = self.start("client")
        operator = self.start("control", await self.event(host, "code"))
        await self.event(host, "prompt")
        host.stop()
        await asyncio.wait_for(self.jobs[0], 2)
        await self.event(operator, "closed")
        self.assertFalse(SyntheticScreen.instances)

    async def test_p2p_video_mouse_voice_and_revoke(self):
        host = self.start("client", threaded=True)
        operator = self.start("control", await self.event(host, "code"), threaded=True)
        future, _ = await self.event(host, "prompt")
        host.answer_prompt(future, True)
        await self.event(operator, "channel_ready")
        await self.event(host, "connected")
        await until(lambda: not operator.frames.empty())
        self.assertEqual(operator.frames.get_nowait().size, (640, 360))
        await until(lambda: bool(AudioSink.instances) and AudioSink.instances[0].frames > 0)
        self.assertIsNone(operator.microphone.stream)

        # Команды до отдельного разрешения не должны доходить до мыши.
        operator.post(operator.send_command, {"type": "down", "x": 0.5, "y": 0.5, "button": "left"})
        await asyncio.sleep(0.1)
        self.assertEqual(host.mouse.commands, [])
        operator.post(operator.toggle_mouse)
        future, _ = await self.event(host, "prompt")
        host.answer_prompt(future, True)
        await until(lambda: operator.mouse_allowed)
        operator.post(operator.send_command, {"type": "down", "x": 0.5, "y": 0.5, "button": "left"})
        await until(lambda: "left" in host.mouse.pressed)
        host.post(host.revoke_mouse)
        await until(lambda: not operator.mouse_allowed)
        self.assertFalse(host.mouse.pressed)
        count = len(host.mouse.commands)
        operator.post(operator.send_command, {"type": "move", "x": 0.8, "y": 0.8})
        await asyncio.sleep(0.1)
        self.assertEqual(len(host.mouse.commands), count)

        # Быстрый повторный запрос получает отказ, а кнопка не зависает.
        operator.post(operator.toggle_mouse)
        await self.event(operator, "mouse_pending")
        await until(lambda: not operator.mouse_request_pending)
        self.assertFalse(operator.mouse_allowed)

        AudioSink.instances[0].nonzero = False
        operator.post(operator.toggle_microphone)
        await until(lambda: operator.microphone.stream is not None)
        await until(lambda: AudioSink.instances[0].nonzero)
        operator.post(operator.toggle_microphone)
        await until(lambda: operator.microphone.stream is None)
        operator.stop()
        await self.event(operator, "closed")
        await self.event(host, "closed")
        self.assertEqual(host.pc.connectionState, "closed")
        self.assertTrue(AudioSink.instances[0].closed)
        self.assertFalse(host.mouse.pressed)
        self.assertEqual(SyntheticScreen.instances[0].readyState, "ended")

    async def test_revoke_cancels_pending_mouse_permission(self):
        host = self.start("client")
        operator = self.start("control", await self.event(host, "code"))
        future, _ = await self.event(host, "prompt")
        host.answer_prompt(future, True)
        await self.event(operator, "channel_ready")
        operator.toggle_mouse()
        future, _ = await self.event(host, "prompt")
        host.revoke_mouse()
        host.answer_prompt(future, True)
        await until(lambda: not host.mouse_request_pending)
        self.assertFalse(host.mouse_allowed)
        self.assertFalse(operator.mouse_allowed)


class MicrophoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_silence_and_does_not_open_device(self):
        with patch("media.sd.RawInputStream") as device:
            track = MicrophoneTrack()
            frame = await track.recv()
            self.assertEqual(frame.time_base, Fraction(1, 48000))
            self.assertFalse(any(bytes(frame.planes[0])))
            device.assert_not_called()
            track.stop()
