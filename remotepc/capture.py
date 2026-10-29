"""Screen capture and image encoding.

The fast path is DXGI Desktop Duplication (``dxcam``) plus OpenCV.  It avoids
the expensive GDI BitBlt + Python resize pipeline and typically lifts a 4K
stream from ~15 fps to the configured cap.  If DXGI or OpenCV is unavailable,
the code falls back to mss + Pillow, so the host remains usable on unusual
systems.
"""

from __future__ import annotations

import atexit
import io
import logging
import math
import os
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .winapi import Monitor

try:
    import mss
except ImportError:  # pragma: no cover - handled by the launcher
    mss = None  # type: ignore[assignment]

try:
    import cv2
    import numpy as np
except ImportError:  # optional acceleration
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

try:
    import dxcam
except ImportError:  # optional acceleration
    dxcam = None  # type: ignore[assignment]

log = logging.getLogger("remotepc.capture")
logging.getLogger("dxcam").setLevel(logging.WARNING)

_local = threading.local()
_cameras_lock = threading.Lock()
_cameras: dict[int, Any] = {}
_dxcam_outputs: dict[str, tuple[int, int]] | None = None


@dataclass(frozen=True)
class CapturedFrame:
    data: bytes
    width: int
    height: int
    crc: int
    unchanged: bool
    codec: str = "jpeg"


def _sct():
    if mss is None:
        raise RuntimeError("Не установлен пакет mss. Запустите setup/start.bat.")
    instance = getattr(_local, "sct", None)
    if instance is None:
        instance = mss.mss()
        _local.sct = instance
    return instance


def _target_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    if max_width <= 0 or width <= max_width:
        return width, height
    ratio = max_width / float(width)
    return max(1, int(round(width * ratio))), max(1, int(round(height * ratio)))


# ---------------------------------------------------------------------------
# DXGI / dxcam backend


def _discover_dxcam_outputs() -> dict[str, tuple[int, int]]:
    """Map ``\\\\.\\DISPLAYx`` to (device_idx, output_idx)."""

    global _dxcam_outputs
    if _dxcam_outputs is not None:
        return _dxcam_outputs
    outputs: dict[str, tuple[int, int]] = {}
    if dxcam is None:
        _dxcam_outputs = outputs
        return outputs
    with _cameras_lock:
        if _dxcam_outputs is not None:
            return _dxcam_outputs
        for device_idx in range(4):
            found_device = False
            for output_idx in range(8):
                camera = None
                try:
                    camera = dxcam.create(
                        device_idx=device_idx,
                        output_idx=output_idx,
                        output_color="BGR",
                        processor_backend="numpy",
                        max_buffer_len=2,
                    )
                    found_device = True
                    output = getattr(camera, "_output", None)
                    name = str(getattr(output, "devicename", "") or "")
                    if not name:
                        # Fallback for dxcam builds that don't expose _output.
                        text = str(camera)
                        marker = "Output Name:"
                        if marker in text:
                            name = text.split(marker, 1)[1].split(" ", 1)[0].strip()
                    if name:
                        outputs[name.upper()] = (device_idx, output_idx)
                except Exception:
                    break
                finally:
                    if camera is not None:
                        try:
                            camera.release()
                        except Exception:
                            pass
            if not found_device:
                break
        _dxcam_outputs = outputs
    if not outputs:
        log.debug("dxcam is installed but no outputs were discovered")
    return outputs


def dxgi_output_index(monitor: Monitor) -> int | None:
    """Return the DXGI output index used by both dxcam and ffmpeg ddagrab."""

    mapping = _discover_dxcam_outputs().get(monitor.device.upper())
    if mapping is None:
        return None
    return mapping[1]


def _dxcam_camera(monitor: Monitor):
    if dxcam is None:
        return None
    cameras = getattr(_local, "cameras", None)
    if cameras is None:
        cameras = {}
        _local.cameras = cameras
    failed = getattr(_local, "dxcam_failed", None)
    if failed is None:
        failed = set()
        _local.dxcam_failed = failed
    if monitor.id in failed:
        return None
    existing = cameras.get(monitor.id)
    if existing is not None:
        if getattr(existing, "is_released", False):
            cameras.pop(monitor.id, None)
        else:
            return existing

    mapping = _discover_dxcam_outputs().get(monitor.device.upper())
    if mapping is None:
        log.info("dxcam: output for %s not found, using mss", monitor.device)
        failed.add(monitor.id)
        return None
    device_idx, output_idx = mapping
    camera = None
    try:
        camera = dxcam.create(
            device_idx=device_idx,
            output_idx=output_idx,
            output_color="BGR",
            processor_backend="numpy",
            max_buffer_len=2,
        )
        camera.start(target_fps=60, video_mode=True)
        cameras[monitor.id] = camera
        with _cameras_lock:
            _cameras[monitor.id] = camera
        log.info("dxcam: capturing %s at %dx%d", monitor.device, camera.width, camera.height)
        return camera
    except Exception as exc:
        log.warning("dxcam camera failed for %s (%s), using mss", monitor.device, exc)
        failed.add(monitor.id)
        if camera is not None:
            try:
                camera.stop()
            except Exception:
                pass
            try:
                camera.release()
            except Exception:
                pass
        return None


def _stop_camera(camera: Any) -> None:
    try:
        camera.stop()
    except Exception:
        pass
    try:
        camera.release()
    except Exception:
        pass


def release_capture() -> None:
    """Stop all DXGI capture threads (also called between viewer sessions)."""

    with _cameras_lock:
        cameras = list(_cameras.values())
        _cameras.clear()
    local_cameras = getattr(_local, "cameras", None)
    if isinstance(local_cameras, dict):
        local_cameras.clear()
    for camera in cameras:
        _stop_camera(camera)


def _grab_dxcam(monitor: Monitor):
    """Return (frame, crc) or False if the DXGI camera is unavailable."""

    camera = _dxcam_camera(monitor)
    if camera is None:
        return False
    try:
        # Always take the latest frame instead of waiting for a new one.  On a
        # static desktop this costs one 4K copy plus CRC, but on an active
        # desktop it removes a full vblank from the frame interval.
        frame = camera.grab(new_frame_only=False)
        if frame is None:
            return False
        try:
            crc = zlib.crc32(memoryview(frame))
        except (TypeError, BufferError):
            crc = zlib.crc32(frame.tobytes())
        return frame, crc
    except Exception as exc:
        log.warning("dxcam grab failed for %s (%s), using mss", monitor.device, exc)
        _local.dxcam_failed.add(monitor.id)
        return False


# ---------------------------------------------------------------------------
# Encoding


def _encode_pil(image: Image.Image, quality: int, codec: str) -> bytes:
    buffer = io.BytesIO()
    if codec == "webp":
        # method=0 is the only WebP setting that is remotely real-time.
        image.save(buffer, format="WEBP", quality=quality, method=0)
    else:
        image.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=False,
            progressive=False,
            subsampling=0 if quality >= 80 else 2,
        )
    return buffer.getvalue()


def _encode_cv2(image, quality: int, codec: str) -> bytes:
    if codec == "webp":
        # OpenCV's libwebp path is slower than Pillow method=0.
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return _encode_pil(Image.fromarray(rgb), quality, codec)

    params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
    if quality >= 80:
        sampling = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444", None)
    else:
        sampling = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_420", None)
    if sampling is not None:
        params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR, sampling]
    ok, encoded = cv2.imencode(".jpg", image, params)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return encoded.tobytes()


def _encode_array(frame: Any, max_width: int, quality: int, codec: str) -> tuple[bytes, int, int]:
    height, width = frame.shape[:2]
    target_w, target_h = _target_size(width, height, max_width)
    if cv2 is not None:
        if (target_w, target_h) != (width, height):
            frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
        return _encode_cv2(frame, quality, codec), target_w, target_h

    image = Image.frombuffer(
        "RGB",
        (width, height),
        frame,
        "raw",
        "BGR",
        frame.strides[0],
        1,
    )
    if (target_w, target_h) != image.size:
        image = image.resize((target_w, target_h), Image.Resampling.BICUBIC)
    return _encode_pil(image, quality, codec), target_w, target_h


def _encode_mss(raw: bytes, width: int, height: int, max_width: int, quality: int, codec: str):
    if cv2 is not None and np is not None:
        frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))[:, :, :3]
        return _encode_array(frame, max_width, quality, codec)
    image = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")
    target_w, target_h = _target_size(width, height, max_width)
    if (target_w, target_h) != image.size:
        image = image.resize((target_w, target_h), Image.Resampling.BICUBIC)
    return _encode_pil(image, quality, codec), target_w, target_h


# ---------------------------------------------------------------------------
# Public capture entry point


def capture_frame(
    monitor: Monitor,
    max_width: int,
    quality: int,
    last_crc: int | None = None,
    demo: bool = False,
    frame_number: int = 0,
    codec: str = "jpeg",
    force: bool = False,
) -> CapturedFrame:
    """Capture one monitor and return an encoded image plus a change marker."""

    quality = max(25, min(95, int(quality)))
    codec = codec if codec in ("jpeg", "webp") else "jpeg"
    if demo:
        image = _demo_image(monitor, max_width, frame_number)
        out = _encode_pil(image, quality, codec)
        return CapturedFrame(out, image.width, image.height, 0, False, codec)

    grabbed = _grab_dxcam(monitor) if dxcam is not None else False
    if grabbed is not False:
        if grabbed is None:
            return CapturedFrame(b"", monitor.width, monitor.height, last_crc or 0, True, codec)
        frame, crc = grabbed
        if not force and last_crc is not None and crc == last_crc:
            return CapturedFrame(b"", monitor.width, monitor.height, crc, True, codec)
        data, width, height = _encode_array(frame, max_width, quality, codec)
        return CapturedFrame(data, width, height, crc, False, codec)

    if mss is None:
        raise RuntimeError("Нет доступного backend захвата экрана.")
    screenshot = _sct().grab(monitor.box)
    raw = screenshot.raw
    crc = zlib.crc32(raw)
    if not force and last_crc is not None and crc == last_crc:
        return CapturedFrame(b"", screenshot.width, screenshot.height, crc, True, codec)

    data, width, height = _encode_mss(
        raw,
        screenshot.width,
        screenshot.height,
        max_width,
        quality,
        codec,
    )
    return CapturedFrame(data, width, height, crc, False, codec)


# Backward-compatible name for callers/tests from the first version.
capture_jpeg = capture_frame


atexit.register(release_capture)


# ---------------------------------------------------------------------------
# Built-in test screen (--demo)


_demo_start = time.monotonic()


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf"),
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _demo_image(monitor: Monitor, max_width: int, frame_number: int) -> Image.Image:
    width, height = _target_size(monitor.width, monitor.height, max_width)
    width, height = max(640, width), max(360, height)
    elapsed = time.monotonic() - _demo_start
    image = Image.new("RGB", (width, height), "#08111d")
    draw = ImageDraw.Draw(image)

    for y in range(height):
        t = y / max(1, height - 1)
        color = (
            int(7 + 12 * (1 - t)),
            int(17 + 28 * (1 - t)),
            int(31 + 38 * (1 - t)),
        )
        draw.line((0, y, width, y), fill=color)

    step = max(36, width // 34)
    for x in range(0, width, step):
        draw.line((x, 0, x, height), fill=(19, 38, 58))
    for y in range(0, height, step):
        draw.line((0, y, width, y), fill=(19, 38, 58))

    panel_w = int(width * 0.68)
    panel_h = int(height * 0.48)
    left = (width - panel_w) // 2
    top = (height - panel_h) // 2
    draw.rounded_rectangle(
        (left, top, left + panel_w, top + panel_h),
        radius=max(18, width // 70),
        fill=(11, 23, 38),
        outline=(49, 209, 181),
        width=max(2, width // 640),
    )

    title_font = _font(max(26, min(72, width // 22)))
    body_font = _font(max(18, min(44, width // 42)))
    mono_font = _font(max(16, min(34, width // 56)))

    title_height = getattr(title_font, "size", max(26, min(72, width // 22)))
    draw.text((left + 42, top + 36), "RemotePC", font=title_font, fill=(231, 255, 249))
    draw.text(
        (left + 42, top + 36 + title_height + 18),
        "Тестовый режим — реальный экран не передаётся",
        font=body_font,
        fill=(133, 168, 190),
    )

    pulse = (math.sin(elapsed * 2.2) + 1) / 2
    bar_w = int((panel_w - 84) * (0.15 + 0.75 * pulse))
    bar_y = top + panel_h - 104
    draw.rounded_rectangle(
        (left + 42, bar_y, left + panel_w - 42, bar_y + 18),
        radius=9,
        fill=(23, 45, 65),
    )
    draw.rounded_rectangle(
        (left + 42, bar_y, left + 42 + bar_w, bar_y + 18),
        radius=9,
        fill=(49, 209, 181),
    )

    dot_x = left + 42 + int((panel_w - 84) * ((elapsed * 0.18) % 1.0))
    dot_y = top + 48
    draw.ellipse((dot_x - 6, dot_y - 6, dot_x + 6, dot_y + 6), fill=(255, 196, 87))

    stamp = time.strftime("%H:%M:%S")
    draw.text(
        (left + 42, top + panel_h - 70),
        f"Монитор {monitor.id}  ·  {monitor.width}×{monitor.height}  ·  {stamp}",
        font=mono_font,
        fill=(157, 190, 208),
    )
    draw.text(
        (left + 42, top + panel_h - 38),
        f"кадр {frame_number}",
        font=mono_font,
        fill=(79, 122, 145),
    )
    return image