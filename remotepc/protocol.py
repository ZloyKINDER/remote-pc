"""Wire format shared by the server and the browser client."""

from __future__ import annotations

import struct

PROTOCOL_VERSION = 1
FRAME_MAGIC = b"RP"
FRAME_HEADER = struct.Struct("<2sBIBHH")  # magic, flags, seq, monitor, width, height
FRAME_HEADER_SIZE = FRAME_HEADER.size
FLAG_JPEG = 1
FLAG_WEBP = 2
FLAG_H264 = 3
FLAG_H264_KEY = 4
FLAG_AUDIO = 5
CODEC_FLAGS = {"jpeg": FLAG_JPEG, "webp": FLAG_WEBP, "h264": FLAG_H264, "audio": FLAG_AUDIO}


def pack_frame(
    seq: int,
    monitor_id: int,
    width: int,
    height: int,
    data: bytes,
    codec: str = "jpeg",
) -> bytes:
    flag = CODEC_FLAGS.get(codec, FLAG_JPEG)
    return FRAME_HEADER.pack(
        FRAME_MAGIC,
        flag,
        seq & 0xFFFFFFFF,
        monitor_id & 0xFF,
        width & 0xFFFF,
        height & 0xFFFF,
    ) + data


def pack_h264_frame(
    seq: int,
    monitor_id: int,
    width: int,
    height: int,
    data: bytes,
    key: bool,
) -> bytes:
    return FRAME_HEADER.pack(
        FRAME_MAGIC,
        FLAG_H264_KEY if key else FLAG_H264,
        seq & 0xFFFFFFFF,
        monitor_id & 0xFF,
        width & 0xFFFF,
        height & 0xFFFF,
    ) + data


def pack_audio_frame(
    seq: int,
    sample_rate: int,
    channels: int,
    data: bytes,
) -> bytes:
    return FRAME_HEADER.pack(
        FRAME_MAGIC,
        FLAG_AUDIO,
        seq & 0xFFFFFFFF,
        0,
        sample_rate & 0xFFFF,
        channels & 0xFFFF,
    ) + data


def unpack_header(data: bytes) -> dict | None:
    if len(data) < FRAME_HEADER_SIZE:
        return None
    magic, flags, seq, monitor_id, width, height = FRAME_HEADER.unpack_from(data)
    if magic != FRAME_MAGIC:
        return None
    if flags == FLAG_AUDIO:
        codec = "audio"
    elif flags in (FLAG_H264, FLAG_H264_KEY):
        codec = "h264"
    elif flags == FLAG_WEBP:
        codec = "webp"
    else:
        codec = "jpeg"
    return {
        "flags": flags,
        "codec": codec,
        "seq": seq,
        "monitor": monitor_id,
        "width": width,
        "height": height,
    }
