"""Захват экрана, микрофон оператора и динамики клиента."""
import asyncio
from fractions import Fraction
import queue
import threading
import time

from aiortc import AudioStreamTrack, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
import av
import mss
from PIL import Image
import sounddevice as sd

import config


def put_latest(target, item):
    try:
        target.put_nowait(item)
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            pass


class ScreenTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self.frames = queue.Queue(maxsize=1)
        self.halt = threading.Event()
        self.monitor = None
        self.error = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._capture, daemon=True)
        self.thread.start()

    def _capture(self):
        try:
            # MSS создаётся и используется в одном потоке (важно на Windows).
            with mss.mss() as capture:
                if not 1 <= config.MONITOR_INDEX < len(capture.monitors):
                    raise ValueError("MONITOR_INDEX не соответствует подключённому монитору")
                self.monitor = dict(capture.monitors[config.MONITOR_INDEX])
                while not self.halt.is_set():
                    tick = time.monotonic()
                    shot = capture.grab(self.monitor)
                    image = Image.frombytes("RGB", shot.size, bytes(shot.bgra), "raw", "BGRX")
                    scale = min(1, config.VIDEO_MAX_WIDTH / image.width)
                    size = (max(2, int(image.width * scale) // 2 * 2),
                            max(2, int(image.height * scale) // 2 * 2))
                    if image.size != size:
                        image = image.resize(size, Image.Resampling.BILINEAR)
                    put_latest(self.frames, image)
                    self.halt.wait(max(0, 1 / config.VIDEO_FPS - (time.monotonic() - tick)))
        except Exception as exc:
            self.error = exc
            self.halt.set()

    async def recv(self):
        while self.readyState == "live":
            if self.error:
                self.stop()
                raise MediaStreamError(str(self.error))
            try:
                image = self.frames.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            frame = av.VideoFrame.from_image(image)
            frame.pts = int((time.monotonic() - self.started) * 90000)
            frame.time_base = Fraction(1, 90000)
            return frame
        raise MediaStreamError

    def stop(self):
        self.halt.set()
        super().stop()


class MicrophoneTrack(AudioStreamTrack):
    """При выключении устройство закрыто; в WebRTC идут только нулевые сэмплы."""
    def __init__(self):
        super().__init__()
        self.stream = None
        self.chunks = queue.Queue(maxsize=5)
        self.pts = 0
        self.started = None

    def enable(self, enabled):
        if self.stream is not None:
            self.stream.abort()
            self.stream.close()
            self.stream = None
        while not self.chunks.empty():
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                break
        if enabled:
            def callback(data, frames, timing, status):
                put_latest(self.chunks, bytes(data))
            stream = sd.RawInputStream(
                samplerate=config.AUDIO_RATE, blocksize=config.AUDIO_SAMPLES,
                channels=1, dtype="int16", device=config.AUDIO_INPUT_DEVICE,
                callback=callback)
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self.stream = stream

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.monotonic()
        if self.started is None or now - (self.started + self.pts / config.AUDIO_RATE) > 0.2:
            self.started = now - self.pts / config.AUDIO_RATE
        await asyncio.sleep(max(0, self.started + self.pts / config.AUDIO_RATE - now))
        try:
            data = self.chunks.get_nowait() if self.stream is not None else b""
        except queue.Empty:
            data = b""
        size = config.AUDIO_SAMPLES * 2
        data = data[:size].ljust(size, b"\x00")
        frame = av.AudioFrame(format="s16", layout="mono", samples=config.AUDIO_SAMPLES)
        frame.planes[0].update(data)
        frame.sample_rate = config.AUDIO_RATE
        frame.time_base = Fraction(1, config.AUDIO_RATE)
        frame.pts = self.pts
        self.pts += config.AUDIO_SAMPLES
        return frame

    def stop(self):
        self.enable(False)
        super().stop()


class Speaker:
    def __init__(self):
        self.chunks = queue.Queue(maxsize=10)
        self.pending = b""
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=config.AUDIO_RATE)

        def callback(outdata, frames, timing, status):
            size = len(outdata)
            while len(self.pending) < size:
                try:
                    self.pending += self.chunks.get_nowait()
                except queue.Empty:
                    break
            outdata[:] = self.pending[:size].ljust(size, b"\x00")
            self.pending = self.pending[size:]

        self.stream = sd.RawOutputStream(
            samplerate=config.AUDIO_RATE, channels=1, dtype="int16",
            blocksize=config.AUDIO_SAMPLES, device=config.AUDIO_OUTPUT_DEVICE,
            callback=callback)
        try:
            self.stream.start()
        except Exception:
            self.stream.close()
            raise

    def push(self, frame):
        for converted in self.resampler.resample(frame):
            # AV plane может содержать выравнивание сверх фактических сэмплов.
            put_latest(self.chunks, bytes(converted.planes[0])[:converted.samples * 2])

    def close(self):
        self.stream.abort()
        self.stream.close()
