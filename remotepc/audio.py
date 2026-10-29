"""System-audio loopback capture via WASAPI (soundcard)."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import threading

log = logging.getLogger("remotepc")

SAMPLE_RATE = 48_000
CHANNELS = 2
CHUNK_FRAMES = 960  # 20 ms


class AudioCapture:
    """Capture the default output device loopback on a worker thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue_size: int = 8) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._record,
            name="remotepc-audio",
            daemon=True,
        )
        self._thread.start()

    def _enqueue(self, pcm: bytes) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def _record(self) -> None:
        com_initialized = False
        try:
            # soundcard/WASAPI uses COM; initialize it on this worker thread.
            hr = ctypes.windll.ole32.CoInitialize(None)
            com_initialized = hr >= 0
            import numpy as np
            import soundcard as sc

            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("устройство вывода по умолчанию не найдено")
            mic = sc.get_microphone(str(speaker.name), include_loopback=True)
            if mic is None:
                raise RuntimeError("loopback-устройство не найдено")
            with mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS) as recorder:
                log.info("audio loopback: %s", speaker.name)
                while not self._stop.is_set():
                    data = recorder.record(numframes=CHUNK_FRAMES)
                    if data is None or len(data) == 0:
                        continue
                    pcm = (
                        np.clip(data, -1.0, 1.0) * 32767.0
                    ).astype("<i2").tobytes()
                    self.loop.call_soon_threadsafe(self._enqueue, pcm)
        except Exception as exc:
            self.error = str(exc)
            log.warning("audio capture disabled: %s", exc)
        finally:
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    async def next_chunk(self) -> bytes:
        return await self.queue.get()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.5)
            self._thread = None
