"""Aiohttp host server: authentication, WebSocket framing and input dispatch."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import secrets
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from . import auth
from .audio import CHANNELS, SAMPLE_RATE, AudioCapture
from .capture import capture_frame, dxgi_output_index, release_capture
from .h264 import AnnexBParser, ffmpeg_args, find_ffmpeg, stream_size
from .protocol import (
    PROTOCOL_VERSION,
    pack_audio_frame,
    pack_frame,
    pack_h264_frame,
)
from .winapi import (
    InputController,
    Monitor,
    get_virtual_screen,
    normalized_to_absolute,
)

log = logging.getLogger("remotepc")

WEB_DIR = Path(__file__).with_name("web")

AUTH_TIMEOUT = 20.0
AUTH_WINDOW = 300.0
AUTH_MAX_FAILURES = 8

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "fullscreen=(self), clipboard-read=(), clipboard-write=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}


@dataclass(frozen=True)
class ServerSettings:
    fps: int = 60
    max_width: int = 3840
    quality: int = 85
    default_width: int = 2560
    codec: str = "jpeg"
    quality_mode: str = "fixed"
    demo: bool = False
    tls: bool = True


@dataclass
class H264Stream:
    monitor_id: int
    process: asyncio.subprocess.Process
    width: int
    height: int
    bitrate: int
    task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    waiting_key: bool = True
    first_frame: asyncio.Event = field(default_factory=asyncio.Event)
    frames_read: int = 0
    keys_read: int = 0
    frames_sent: int = 0
    sps: bytes = b""
    pps: bytes = b""


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    if isinstance(response, web.StreamResponse) and not isinstance(response, web.WebSocketResponse):
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-store"
        elif request.path in ("/app.js", "/style.css"):
            response.headers["Cache-Control"] = "no-cache"
    return response


class RemotePCServer:
    def __init__(self, password: str, monitors: list[Monitor], settings: ServerSettings):
        if not monitors:
            raise ValueError("no monitors found")
        self.settings = settings
        self.monitors = monitors
        self.monitors_by_id = {monitor.id: monitor for monitor in monitors}
        self.virtual = get_virtual_screen()
        self.salt = auth.new_salt()
        self.key = auth.derive_key(password, self.salt)
        self.hostname = socket.gethostname()
        self._active: ViewerSession | None = None
        self._active_lock = asyncio.Lock()
        self._auth_failures: dict[str, collections.deque[float]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self._shutting_down = False

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024, middlewares=[security_headers])
        app.router.add_get("/ws", self.websocket)
        for route, (filename, content_type) in STATIC_FILES.items():
            app.router.add_get(route, self._make_static(filename, content_type))
        app.on_shutdown.append(self._on_shutdown)
        return app

    def _make_static(self, filename: str, content_type: str):
        async def handler(_request: web.Request) -> web.StreamResponse:
            path = WEB_DIR / filename
            if not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers={"Content-Type": content_type})

        return handler

    # -- auth helpers --------------------------------------------------
    def _allow_attempt(self, peer: str) -> bool:
        now = time.monotonic()
        attempts = self._auth_failures.get(peer)
        if not attempts:
            return True
        while attempts and now - attempts[0] > AUTH_WINDOW:
            attempts.popleft()
        if not attempts:
            self._auth_failures.pop(peer, None)
            return True
        return len(attempts) < AUTH_MAX_FAILURES

    def _record_failure(self, peer: str) -> None:
        self._auth_failures.setdefault(peer, collections.deque()).append(time.monotonic())

    def _clear_failures(self, peer: str) -> None:
        self._auth_failures.pop(peer, None)

    async def _detach(self, session: "ViewerSession") -> None:
        async with self._active_lock:
            if self._active is session:
                self._active = None

    # -- HTTP ----------------------------------------------------------
    async def websocket(self, request: web.Request) -> web.StreamResponse:
        if self._shutting_down:
            raise web.HTTPServiceUnavailable(text="server is shutting down")

        origin = request.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != request.host.lower():
                log.warning("rejected websocket from foreign origin %s", origin)
                raise web.HTTPForbidden(text="origin not allowed")

        peer = request.remote or "unknown"
        if not self._allow_attempt(peer):
            ws = web.WebSocketResponse(heartbeat=20, compress=False)
            await ws.prepare(request)
            await ws.send_json(
                {"type": "error", "message": "Слишком много неверных попыток. Подождите 5 минут."}
            )
            await ws.close(code=4008, message="rate limited")
            return ws

        ws = web.WebSocketResponse(
            heartbeat=25,
            autoping=True,
            compress=False,
            max_msg_size=1024 * 1024,
        )
        await ws.prepare(request)

        server_nonce = auth.new_nonce()
        await ws.send_json(
            {
                "type": "hello",
                "version": PROTOCOL_VERSION,
                "salt": auth.b64(self.salt),
                "iterations": auth.ITERATIONS,
                "nonce": auth.b64(server_nonce),
                "tls": self.settings.tls,
            }
        )

        session: ViewerSession | None = None
        try:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=AUTH_TIMEOUT)
            except asyncio.TimeoutError:
                await ws.close(code=4001, message="auth timeout")
                return ws

            if msg.type != WSMsgType.TEXT:
                await ws.close(code=4001, message="auth expected")
                return ws
            try:
                payload = json.loads(msg.data)
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict) or payload.get("type") != "auth":
                await ws.close(code=4001, message="auth expected")
                return ws

            try:
                client_nonce = auth.unb64(payload.get("client_nonce", ""))
                received_proof = auth.unb64(payload.get("proof", ""))
                if len(client_nonce) < 16 or len(client_nonce) > 64 or len(received_proof) != 32:
                    raise ValueError("bad auth values")
            except (ValueError, UnicodeError):
                self._record_failure(peer)
                await self._reject_auth(ws, "Некорректный запрос авторизации.")
                return ws

            if not auth.verify(self.key, server_nonce, client_nonce, received_proof):
                self._record_failure(peer)
                log.warning("failed authentication from %s", peer)
                await self._reject_auth(ws, "Неверный пароль.")
                return ws

            self._clear_failures(peer)

            async with self._active_lock:
                if self._active is not None and not self._active.closed:
                    await ws.send_json(
                        {"type": "busy", "message": "К этому компьютеру уже подключён другой пользователь."}
                    )
                    await ws.close(code=4009, message="busy")
                    return ws
                session = ViewerSession(self, ws, peer)
                self._active = session

            await ws.send_json(
                {
                    "type": "auth_ok",
                    "version": PROTOCOL_VERSION,
                    "host": self.hostname,
                    "monitors": [monitor.as_public() for monitor in self.monitors],
                    "fps": self.settings.fps,
                    "max_width": self.settings.max_width,
                    "stream": {
                        "codec": self.settings.codec,
                        "quality_mode": self.settings.quality_mode,
                        "quality": self.settings.quality,
                        "width": min(self.settings.default_width, self.settings.max_width),
                        "fps": self.settings.fps,
                    },
                    "tls": self.settings.tls,
                }
            )
            log.info("viewer connected from %s", peer)
            await session.run()
            return ws
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("websocket handler failed")
            return ws
        finally:
            if session is not None:
                await session.close()

    async def _reject_auth(self, ws: web.WebSocketResponse, message: str) -> None:
        try:
            await ws.send_json({"type": "auth_fail", "message": message})
        except Exception:
            pass
        await asyncio.sleep(0.8)
        try:
            await ws.close(code=4003, message="authentication failed")
        except Exception:
            pass

    async def _on_shutdown(self, _app: web.Application) -> None:
        self._shutting_down = True
        session = self._active
        if session is not None:
            await session.close(notify=True)
        self._executor.shutdown(wait=False, cancel_futures=True)


class ViewerSession:
    def __init__(self, server: RemotePCServer, ws: web.WebSocketResponse, peer: str):
        self.server = server
        self.ws = ws
        self.peer = peer
        self.monitor_id = server.monitors[0].id
        self.closed = False
        self.screen_ready = False
        self.generation = 0
        self.frame_inflight = False
        self.seq = 0
        self.last_crc: dict[int, int] = {}
        self.last_frame_sent = 0.0
        self.last_frame_started = 0.0
        self.image_sent_at = 0.0
        self.awaiting_ack = False
        self.codec = server.settings.codec
        self.fps = max(5, min(60, int(server.settings.fps)))
        self.quality_mode = server.settings.quality_mode
        self.quality = float(server.settings.quality)
        self.requested_width = min(server.settings.default_width, server.settings.max_width)
        self.force_next_frame = True
        self._next_count = 0
        self._last_next_at = 0.0
        self.h264_streams: dict[int, H264Stream] = {}
        self.h264_signature: tuple[int, int, int] | None = None
        self.h264_paused = False
        self.h264_watchdog = 0
        self.h264_fps = self.fps
        self.h264_bitrate = 20_000_000
        self.audio: AudioCapture | None = None
        self.audio_task: asyncio.Task | None = None
        self.input = InputController(server.virtual)
        self.send_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.frames_sent = 0
        self.bytes_sent = 0

    async def run(self) -> None:
        self._start_audio()
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.info("websocket error from %s: %s", self.peer, self.ws.exception())
                    break
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
        finally:
            await self.close()

    def _start_audio(self) -> None:
        if self.closed or self.audio_task is not None:
            return
        loop = asyncio.get_running_loop()
        self.audio = AudioCapture(loop)
        self.audio.start()
        self.audio_task = asyncio.create_task(self._send_audio())

    async def _send_audio(self) -> None:
        assert self.audio is not None
        try:
            while not self.closed:
                pcm = await self.audio.next_chunk()
                if self.closed:
                    return
                seq = self.seq
                self.seq += 1
                payload = pack_audio_frame(seq, SAMPLE_RATE, CHANNELS, pcm)
                await self._send_bytes(payload)
                self.bytes_sent += len(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("audio sender stopped", exc_info=True)

    async def close(self, notify: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        if self.audio_task is not None:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass
            self.audio_task = None
        if self.audio is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.audio.stop
                )
            except Exception:
                log.debug("audio stop failed", exc_info=True)
            self.audio = None
        try:
            await self._stop_h264()
        except Exception:
            log.debug("failed to stop H.264 stream", exc_info=True)
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            # Run on the capture worker's queue so we never release a camera
            # while the worker is still reading its latest frame.
            await asyncio.get_running_loop().run_in_executor(self.server._executor, release_capture)
        except Exception:
            log.debug("failed to stop capture backend via executor", exc_info=True)
            try:
                release_capture()
            except Exception:
                log.debug("failed to stop capture backend", exc_info=True)
        try:
            self.input.release_all()
        except Exception:
            log.debug("failed to release input", exc_info=True)
        await self.server._detach(self)
        if notify:
            try:
                await self.ws.close(code=1001, message="server shutdown")
            except Exception:
                pass
        if self.frames_sent:
            log.info(
                "viewer %s disconnected (%d frames, %.1f MB)",
                self.peer,
                self.frames_sent,
                self.bytes_sent / (1024 * 1024),
            )

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self.send_lock:
            if self.closed:
                return
            await self.ws.send_json(payload)

    async def _send_bytes(self, payload: bytes) -> None:
        if self.closed:
            return
        async with self.send_lock:
            if self.closed:
                return
            await self.ws.send_bytes(payload)

    # -- incoming messages --------------------------------------------
    async def _on_text(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "next":
            if self.codec == "h264":
                return
            now = time.perf_counter()
            self._next_count += 1
            if self._next_count % 20 == 0:
                log.debug(
                    "next %d: dt %.1fms inflight=%s ack_pending=%s",
                    self._next_count,
                    (now - self._last_next_at) * 1000 if self._last_next_at else 0.0,
                    self.frame_inflight,
                    self.awaiting_ack,
                )
            self._last_next_at = now
            if self.awaiting_ack:
                elapsed = now - self.image_sent_at
                # A hidden tab can pause the loop for minutes; don't punish
                # image quality for that.
                if elapsed < 1.5:
                    self._adapt_quality(elapsed)
                self.awaiting_ack = False
            self._request_frame()
        elif kind == "view":
            await self._on_view(data)
        elif kind == "stream":
            await self._on_stream(data)
        elif kind == "ping":
            await self._send_json({"type": "pong", "t": data.get("t")})
        elif kind == "mouse":
            try:
                self._on_mouse(data)
            except Exception as exc:
                log.warning("mouse injection failed: %s", exc)
        elif kind == "key":
            try:
                self._on_key(data)
            except Exception as exc:
                log.warning("key injection failed: %s", exc)
        elif kind == "text":
            try:
                self._on_text_input(data)
            except Exception as exc:
                log.warning("text injection failed: %s", exc)
        elif kind == "shortcut":
            try:
                self._on_shortcut(data)
            except Exception as exc:
                log.warning("shortcut injection failed: %s", exc)
        elif kind == "release_all":
            try:
                self.input.release_all()
            except Exception as exc:
                log.warning("input release failed: %s", exc)
        # Unknown message types are ignored on purpose (forward compatibility).

    def _effective_width(self, monitor: Monitor) -> int:
        cap = min(monitor.width, self.server.settings.max_width)
        if self.requested_width <= 0:
            return max(1, cap)
        return max(1, min(self.requested_width, cap))

    def _h264_size(self, monitor: Monitor) -> tuple[int, int]:
        cap = min(monitor.width, self.server.settings.max_width)
        requested = self.requested_width if self.requested_width > 0 else cap
        return stream_size(monitor, min(requested, cap))

    def _h264_bitrate(self) -> int:
        if self.quality_mode == "auto":
            return 20_000_000
        return {
            60: 8_000_000,
            75: 15_000_000,
            85: 25_000_000,
            92: 35_000_000,
        }.get(int(round(self.quality)), 20_000_000)

    async def _send_stream_ok(self, monitor: Monitor, warning: str | None = None) -> None:
        if self.codec == "h264":
            width, height = self._h264_size(monitor)
            self.h264_width, self.h264_height = width, height
            self.h264_bitrate = self._h264_bitrate()
            self.h264_fps = self.fps
            payload: dict[str, Any] = {
                "type": "stream_ok",
                "codec": "h264",
                "quality_mode": self.quality_mode,
                "quality": round(self.quality),
                "width": width,
                "height": height,
                "bitrate": self.h264_bitrate,
                "fps": self.h264_fps,
                "requested_width": self.requested_width,
                "max_width": self.server.settings.max_width,
            }
        else:
            payload = {
                "type": "stream_ok",
                "codec": self.codec,
                "quality_mode": self.quality_mode,
                "quality": round(self.quality),
                "width": self._effective_width(monitor),
                "fps": self.fps,
                "requested_width": self.requested_width,
                "max_width": self.server.settings.max_width,
            }
        if warning:
            payload["warning"] = warning
        await self._send_json(payload)

    async def _cancel_jpeg_tasks(self) -> None:
        self.generation += 1
        self.frame_inflight = False
        tasks = [task for task in list(self.tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.get_running_loop().run_in_executor(
                self.server._executor, release_capture
            )
        except Exception:
            log.debug("failed to stop DXGI camera for H.264", exc_info=True)

    async def _stop_h264(self) -> None:
        self.h264_watchdog += 1
        self.h264_signature = None
        streams = list(self.h264_streams.values())
        if not streams:
            return
        self.generation += 1
        self.h264_streams.clear()
        for stream in streams:
            if stream.task is not None:
                stream.task.cancel()
            if stream.stderr_task is not None:
                stream.stderr_task.cancel()
            if stream.process.returncode is None:
                try:
                    stream.process.kill()
                except ProcessLookupError:
                    pass
        for stream in streams:
            if stream.task is not None:
                try:
                    await asyncio.wait_for(stream.task, 0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    log.debug("H.264 reader stopped with error", exc_info=True)
            if stream.stderr_task is not None:
                try:
                    await asyncio.wait_for(stream.stderr_task, 0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            if stream.process.returncode is None:
                try:
                    await asyncio.wait_for(stream.process.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass

    async def _spawn_h264_stream(
        self, monitor: Monitor, bitrate: int, fps: int, generation: int
    ) -> H264Stream | None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return None
        output_index = dxgi_output_index(monitor)
        if output_index is None:
            log.warning("H.264 requested, but no DXGI output for %s", monitor.device)
            return None
        width, height = self._h264_size(monitor)
        scale_w = width if width < monitor.width else 0
        scale_h = height if width < monitor.width else 0
        args = ffmpeg_args(ffmpeg, output_index, scale_w, scale_h, bitrate, fps)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            log.exception("failed to start ffmpeg for %s", monitor.device)
            return None
        stream = H264Stream(
            monitor_id=monitor.id,
            process=process,
            width=width,
            height=height,
            bitrate=bitrate,
        )
        stream.task = asyncio.create_task(self._read_h264(stream, generation))
        stream.stderr_task = asyncio.create_task(self._drain_h264_stderr(process))
        log.info(
            "H.264 stream start: monitor=%d %dx%d %d fps %.1f Mbps output=%d",
            monitor.id,
            width,
            height,
            fps,
            bitrate / 1_000_000,
            output_index,
        )
        return stream

    async def _start_h264(self, monitor: Monitor, force: bool = False) -> bool:
        self.h264_watchdog += 1
        if self.closed or self.server.settings.demo:
            return False
        bitrate = self._h264_bitrate()
        signature = (self.requested_width, bitrate, self.fps)
        if not force and self.h264_streams and self.h264_signature == signature:
            stream = self.h264_streams.get(monitor.id)
            if stream is not None and stream.process.returncode is None:
                return True
        await self._stop_h264()
        if find_ffmpeg() is None:
            log.warning("H.264 requested, but ffmpeg.exe was not found")
            return False
        await self._cancel_jpeg_tasks()
        # Give DXGI/NVENC a moment to release the previous duplication and
        # encoder session before starting new ones.
        await asyncio.sleep(0.2)
        self.h264_signature = signature
        self.h264_bitrate = bitrate
        self.h264_fps = self.fps
        generation = self.generation
        active = await self._spawn_h264_stream(monitor, bitrate, self.fps, generation)
        if active is None:
            await self._stop_h264()
            return False
        self.h264_streams[active.monitor_id] = active
        first_task = asyncio.create_task(active.first_frame.wait())
        exit_task = asyncio.create_task(active.process.wait())
        ready = False
        try:
            done, _pending = await asyncio.wait(
                {first_task, exit_task},
                timeout=7.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            ready = first_task in done and first_task.result()
        finally:
            for task in (first_task, exit_task):
                if not task.done():
                    task.cancel()
        if not ready:
            log.warning("H.264 stream produced no first frame or ffmpeg exited")
            await self._stop_h264()
            return False
        return True

    async def _drain_h264_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                log.debug("ffmpeg: %s", text)

    async def _read_h264(self, stream: H264Stream, generation: int) -> None:
        if stream.process.stdout is None:
            return
        monitor_id = stream.monitor_id
        parser = AnnexBParser()
        try:
            while not self.closed and generation == self.generation:
                data = await stream.process.stdout.read(65536)
                if not data:
                    break
                for access_unit, key in parser.feed(data):
                    if self.closed or generation != self.generation:
                        return
                    if parser.sps:
                        stream.sps = parser.sps
                    if parser.pps:
                        stream.pps = parser.pps
                    stream.frames_read += 1
                    if key:
                        stream.keys_read += 1
                    if not stream.first_frame.is_set():
                        stream.first_frame.set()
                    active = (
                        self.codec == "h264"
                        and not self.h264_paused
                        and self.monitor_id == monitor_id
                    )
                    if not active:
                        stream.waiting_key = True
                        continue
                    if stream.waiting_key:
                        if not key:
                            continue
                        stream.waiting_key = False
                        log.info("H.264 keyframe monitor=%d", monitor_id)
                    await self._send_h264(stream, access_unit, key)
            for access_unit, key in parser.flush():
                if self.closed or generation != self.generation:
                    return
                if parser.sps:
                    stream.sps = parser.sps
                if parser.pps:
                    stream.pps = parser.pps
                stream.frames_read += 1
                if not stream.first_frame.is_set():
                    stream.first_frame.set()
                if (
                    self.codec == "h264"
                    and not self.h264_paused
                    and self.monitor_id == monitor_id
                    and not stream.waiting_key
                ):
                    await self._send_h264(stream, access_unit, key)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("H.264 reader failed")
        finally:
            if (
                not self.closed
                and generation == self.generation
                and self.codec == "h264"
                and self.monitor_id == monitor_id
            ):
                log.warning("H.264 stream ended unexpectedly")
                self.codec = "jpeg"
                monitor = self.server.monitors_by_id[self.monitor_id]
                asyncio.create_task(
                    self._send_stream_ok(monitor, "H.264 остановился, включён JPEG.")
                )
                self._request_frame()

    def _arm_h264_watchdog(self, monitor_id: int) -> None:
        self.h264_watchdog += 1
        token = self.h264_watchdog

        async def check() -> None:
            await asyncio.sleep(2.0)
            if (
                token != self.h264_watchdog
                or self.closed
                or self.codec != "h264"
                or self.monitor_id != monitor_id
            ):
                return
            stream = self.h264_streams.get(monitor_id)
            if stream is None or not stream.waiting_key:
                return
            log.warning(
                "no H.264 keyframe for monitor %d; frames=%d keys=%d sent=%d paused=%s active=%d",
                monitor_id,
                stream.frames_read,
                stream.keys_read,
                stream.frames_sent,
                self.h264_paused,
                self.monitor_id,
            )
            self.codec = "jpeg"
            await self._stop_h264()
            monitor = self.server.monitors_by_id[monitor_id]
            await self._send_stream_ok(monitor, "H.264 не ответил, включён JPEG.")
            self._request_frame()

        asyncio.create_task(check())

    async def _send_h264(self, stream: H264Stream, access_unit: bytes, key: bool) -> None:
        if not access_unit:
            return
        data = access_unit
        if (
            key
            and stream.sps
            and stream.pps
            and b"\x00\x00\x01\x67" not in access_unit
            and b"\x00\x00\x00\x01\x67" not in access_unit
        ):
            # The encoder emits SPS/PPS mainly at the start of the stream.
            # An inactive stream's opening headers are deliberately drained,
            # so repeat them before a keyframe when a viewer attaches/switches.
            data = stream.sps + stream.pps + access_unit
        seq = self.seq
        self.seq += 1
        payload = pack_h264_frame(
            seq,
            stream.monitor_id,
            stream.width,
            stream.height,
            data,
            key,
        )
        stream.frames_sent += 1
        await self._send_bytes(payload)
        self.last_frame_sent = time.perf_counter()
        self.frames_sent += 1
        self.bytes_sent += len(payload)
        stream.first_frame.set()

    async def _on_stream(self, data: dict[str, Any]) -> None:
        codec = data.get("codec", self.codec)
        mode = data.get("quality_mode", self.quality_mode)
        if codec not in ("jpeg", "webp", "h264") or mode not in ("auto", "fixed"):
            return
        try:
            quality = int(data.get("quality", self.quality))
            width = int(data.get("width", self.requested_width))
            fps = int(data.get("fps", self.fps))
        except (TypeError, ValueError):
            return

        self.codec = codec
        self.fps = max(5, min(60, fps))
        self.quality_mode = mode
        if mode == "auto":
            self.quality = float(max(55, min(92, quality)))
        else:
            self.quality = float(max(40, min(95, quality)))
        if width != 0:
            width = max(320, min(width, self.server.settings.max_width))
        self.requested_width = width

        await self._stop_h264()
        self.generation += 1
        self.frame_inflight = False
        self.awaiting_ack = False
        self.force_next_frame = True
        self.last_crc.clear()

        monitor = self.server.monitors_by_id[self.monitor_id]
        self.h264_paused = False
        if codec == "h264":
            await self._send_stream_ok(monitor)
            if not self.screen_ready:
                return
            if not await self._start_h264(monitor):
                self.codec = "jpeg"
                await self._send_stream_ok(monitor, "H.264 недоступен, включён JPEG.")
                self._request_frame()
            else:
                self._arm_h264_watchdog(monitor.id)
            return

        await self._send_stream_ok(monitor)
        self._request_frame()

    async def _on_view(self, data: dict[str, Any]) -> None:
        try:
            monitor_id = int(data.get("monitor"))
        except (TypeError, ValueError):
            return
        monitor = self.server.monitors_by_id.get(monitor_id)
        if monitor is None:
            return
        previous_monitor = self.monitor_id
        if self.codec == "h264":
            self.h264_paused = True
        self.monitor_id = monitor.id
        log.info(
            "view monitor=%d codec=%s streams=%s paused=%s",
            monitor.id,
            self.codec,
            sorted(self.h264_streams),
            self.h264_paused,
        )
        self.generation += 1
        self.frame_inflight = False
        self.screen_ready = True
        self.last_crc.clear()
        self.awaiting_ack = False
        self.force_next_frame = True
        self.input.release_all()
        await self._send_json(
            {
                "type": "screen",
                "monitor": monitor.id,
                "name": monitor.name,
                "width": monitor.width,
                "height": monitor.height,
                "stream_width": (
                    self._h264_size(monitor)[0]
                    if self.codec == "h264"
                    else self._effective_width(monitor)
                ),
            }
        )
        if self.codec == "h264":
            existing = self.h264_streams.get(monitor.id)
            if existing is not None:
                log.debug(
                    "target monitor=%d frames_read=%d frames_sent=%d first=%s waiting=%s rc=%s",
                    monitor.id,
                    existing.frames_read,
                    existing.frames_sent,
                    existing.first_frame.is_set(),
                    existing.waiting_key,
                    existing.process.returncode,
                )
                existing.waiting_key = True
            await self._send_stream_ok(monitor)
            # A prewarmed stream for another monitor can stall after its
            # opening headers were drained.  Restarting the target encoder is
            # the reliable path; it is still much faster than asking the user
            # to re-select H.264 manually.
            force = previous_monitor is not None and monitor.id != previous_monitor
            if not await self._start_h264(monitor, force=force):
                self.codec = "jpeg"
                await self._send_stream_ok(monitor, "H.264 недоступен, включён JPEG.")
                self._request_frame()
            self.h264_paused = False
            self._arm_h264_watchdog(monitor.id)
            return
        self._request_frame()

    def _request_frame(self) -> None:
        if self.closed or not self.screen_ready or self.frame_inflight:
            return
        self.frame_inflight = True
        generation = self.generation
        task = asyncio.create_task(self._send_one_frame(generation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _send_one_frame(self, generation: int) -> None:
        try:
            if generation != self.generation or self.closed:
                return
            fps = max(1, min(60, self.fps))
            now = time.perf_counter()
            wait = (1.0 / fps) - (now - self.last_frame_started) if self.last_frame_started else 0.0
            if wait > 0:
                await asyncio.sleep(wait)
            if generation != self.generation or self.closed:
                return
            self.last_frame_started = time.perf_counter()
            monitor = self.server.monitors_by_id[self.monitor_id]
            crc = self.last_crc.get(monitor.id)
            frame_started = time.perf_counter()
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self.server._executor,
                capture_frame,
                monitor,
                self._effective_width(monitor),
                int(self.quality),
                crc,
                self.server.settings.demo,
                self.frames_sent,
                self.codec,
                self.force_next_frame,
            )
            captured_at = time.perf_counter()
            if generation != self.generation or self.closed:
                return
            if result.unchanged:
                self.last_frame_sent = time.perf_counter()
                self.awaiting_ack = False
                await self._send_json({"type": "nochange"})
                return

            seq = self.seq
            self.seq += 1
            payload = pack_frame(seq, monitor.id, result.width, result.height, result.data, result.codec)
            await self._send_bytes(payload)
            self.last_crc[monitor.id] = result.crc
            self.last_frame_sent = time.perf_counter()
            if self.frames_sent % 20 == 0:
                log.debug(
                    "frame %d: capture+encode %.1fms send %.1fms bytes %d",
                    self.frames_sent,
                    (captured_at - frame_started) * 1000,
                    (self.last_frame_sent - captured_at) * 1000,
                    len(payload),
                )
            # Elapsed time for quality control covers capture, encoding,
            # transport backpressure, client decode and the ack round trip.
            self.image_sent_at = frame_started
            self.awaiting_ack = True
            self.force_next_frame = False
            self.frames_sent += 1
            self.bytes_sent += len(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("frame capture failed")
            if generation == self.generation and not self.closed:
                self.last_frame_sent = time.perf_counter()
                message = "Не удалось захватить экран. Проверьте, что ПК разблокирован."
                if "mss" in str(exc).lower():
                    message = "Ошибка захвата экрана (mss). Перезапустите хост."
                try:
                    await self._send_json({"type": "error", "message": message})
                except Exception:
                    log.debug("failed to send capture error", exc_info=True)
        finally:
            if generation == self.generation:
                self.frame_inflight = False

    def _adapt_quality(self, elapsed: float) -> None:
        if self.quality_mode != "auto":
            return
        # elapsed = capture+encode+send+decode+ack.  Keep quality high; the
        # viewer can pick a lower profile explicitly.
        if elapsed > 0.35:
            self.quality -= 6
        elif elapsed > 0.22:
            self.quality -= 3
        elif elapsed > 0.12:
            self.quality -= 1
        elif elapsed < 0.045:
            self.quality += 1.0
        self.quality = max(55.0, min(92.0, self.quality))

    # -- input ---------------------------------------------------------
    def _coordinates(self, data: dict[str, Any]) -> tuple[float, float] | None:
        try:
            x = float(data.get("x"))
            y = float(data.get("y"))
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        if x < -0.02 or x > 1.02 or y < -0.02 or y > 1.02:
            return None
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    def _on_mouse(self, data: dict[str, Any]) -> None:
        action = data.get("action")
        if action == "wheel":
            try:
                dx = int(data.get("dx", 0))
                dy = int(data.get("dy", 0))
            except (TypeError, ValueError):
                return
            self.input.wheel(max(-10, min(10, dx)), max(-10, min(10, dy)))
            return

        coords = self._coordinates(data)
        if coords is None:
            # Releasing a button must not depend on a valid canvas coordinate.
            if action == "up":
                button = str(data.get("button", "left"))
                if button in {"left", "right", "middle", "back", "forward"}:
                    self.input.mouse_button(button, False)
            return
        monitor = self.server.monitors_by_id[self.monitor_id]
        px, py = normalized_to_absolute(coords[0], coords[1], monitor, self.server.virtual)
        self.input.move_to_pixels(px, py)
        if action == "down" or action == "up":
            button = str(data.get("button", "left"))
            if button not in {"left", "right", "middle", "back", "forward"}:
                return
            self.input.mouse_button(button, action == "down")

    def _on_key(self, data: dict[str, Any]) -> None:
        code = data.get("code")
        down = data.get("down")
        if not isinstance(code, str) or len(code) > 32 or not isinstance(down, bool):
            return
        self.input.key(code, down)

    def _on_text_input(self, data: dict[str, Any]) -> None:
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return
        self.input.text(text[:512])

    def _on_shortcut(self, data: dict[str, Any]) -> None:
        name = data.get("name")
        if not isinstance(name, str):
            return
        ok, message = self.input.shortcut(name)
        if not ok and message:
            asyncio.create_task(self._send_toast(message))

    async def _send_toast(self, message: str) -> None:
        try:
            await self._send_json({"type": "toast", "message": message})
        except Exception:
            log.debug("failed to send toast", exc_info=True)
