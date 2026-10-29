"""H.264 low-latency stream helpers (NVENC via ffmpeg)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .winapi import Monitor


def find_ffmpeg() -> str | None:
    candidates = [
        Path(__file__).resolve().parent.parent / "ffmpeg.exe",
        Path.cwd() / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg")


START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")


def _nal_type(nal: bytes) -> int | None:
    if nal.startswith(b"\x00\x00\x00\x01"):
        index = 4
    elif nal.startswith(b"\x00\x00\x01"):
        index = 3
    else:
        return None
    if index >= len(nal):
        return None
    return nal[index] & 0x1F


class AnnexBParser:
    """Split an H.264 Annex B byte stream into access units."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.current: list[bytes] = []
        self.has_vcl = False
        self.sps = b""
        self.pps = b""

    def feed(self, data: bytes) -> list[tuple[bytes, bool]]:
        self.buffer.extend(data)
        units: list[tuple[bytes, bool]] = []
        matches = list(START_CODE.finditer(self.buffer))
        if len(matches) < 2:
            if matches:
                # Keep the last complete start code for the next chunk.
                del self.buffer[: matches[-1].start()]
            elif len(self.buffer) > 4:
                # Keep a possible split start code.
                del self.buffer[:-4]
            return units
        for index in range(len(matches) - 1):
            start = matches[index].start()
            end = matches[index + 1].start()
            units.extend(self._add_nal(bytes(self.buffer[start:end])))
        del self.buffer[: matches[-1].start()]
        return units

    def _flush_current(self) -> list[tuple[bytes, bool]]:
        if not self.current:
            return []
        data = b"".join(self.current)
        key = any(_nal_type(nal) == 5 for nal in self.current)
        self.current = []
        self.has_vcl = False
        return [(data, key)]

    def flush(self) -> list[tuple[bytes, bool]]:
        units: list[tuple[bytes, bool]] = []
        if self.buffer:
            start = START_CODE.search(self.buffer)
            payload = bytes(self.buffer[start.start() :]) if start is not None else b""
            self.buffer.clear()
            if payload:
                units.extend(self._add_nal(payload))
        units.extend(self._flush_current())
        return units

    def _add_nal(self, nal: bytes) -> list[tuple[bytes, bool]]:
        nal_type = _nal_type(nal)
        if nal_type == 7:
            self.sps = nal
        elif nal_type == 8:
            self.pps = nal
        units: list[tuple[bytes, bool]] = []
        boundary = nal_type in (1, 5, 6, 7, 8, 9)
        if boundary and self.has_vcl:
            units.extend(self._flush_current())
        self.current.append(nal)
        if nal_type in (1, 5):
            self.has_vcl = True
        return units


def ffmpeg_args(
    ffmpeg: str,
    output_index: int,
    width: int,
    height: int,
    bitrate: int,
    fps: int = 60,
) -> list[str]:
    filters = "hwdownload,format=bgra"
    if width > 0 and height > 0:
        filters += f",scale={width}:{height}:flags=fast_bilinear"
    # Keep keyframes roughly a quarter-second apart even at 15/30 fps.
    gop = max(1, min(15, max(1, fps // 4)))
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "lavfi",
        "-i",
        f"ddagrab=output_idx={output_index}:framerate={fps}:draw_mouse=0",
        "-vf",
        filters,
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p1",
        "-tune",
        "ll",
        "-zerolatency",
        "1",
        "-delay",
        "0",
        "-rc-lookahead",
        "0",
        "-forced-idr",
        "1",
        "-no-scenecut",
        "1",
        "-rc",
        "cbr",
        "-b:v",
        str(bitrate),
        "-maxrate",
        str(bitrate),
        "-bufsize",
        str(max(1_000_000, bitrate // 2)),
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-bf",
        "0",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "h264",
        "pipe:1",
    ]


def stream_size(monitor: Monitor, requested_width: int) -> tuple[int, int]:
    if requested_width <= 0 or requested_width >= monitor.width:
        width, height = monitor.width, monitor.height
    else:
        width = requested_width
        height = max(2, int(round(monitor.height * width / monitor.width)))
    # yuv420p requires even dimensions
    return width - (width % 2), height - (height % 2)
