from __future__ import annotations

import asyncio
import time
import unittest

from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestServer

from remotepc import auth
from remotepc.protocol import unpack_header
from remotepc.server import RemotePCServer, ServerSettings
from remotepc.winapi import enable_dpi_awareness, get_monitors


async def receive_json(ws, timeout: float) -> dict:
    """Read the next JSON message, ignoring interleaved audio packets."""
    import json

    while True:
        msg = await ws.receive(timeout=timeout)
        if msg.type == WSMsgType.TEXT:
            return json.loads(msg.data)


async def authenticate(ws, password: str) -> dict:
    hello = await ws.receive_json(timeout=5)
    assert hello["type"] == "hello", hello
    salt = auth.unb64(hello["salt"])
    server_nonce = auth.unb64(hello["nonce"])
    key = auth.derive_key(password, salt, hello["iterations"])
    client_nonce = auth.new_nonce()
    proof = auth.proof(key, server_nonce, client_nonce)
    await ws.send_json(
        {
            "type": "auth",
            "client_nonce": auth.b64(client_nonce),
            "proof": auth.b64(proof),
        }
    )
    return await ws.receive_json(timeout=10)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        enable_dpi_awareness()
        monitors = get_monitors()
        self.server = RemotePCServer(
            "secret-123",
            monitors,
            ServerSettings(fps=20, max_width=960, quality=60, demo=True, tls=False),
        )
        self.http = TestServer(self.server.make_app())
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()
        self.server._executor.shutdown(wait=False, cancel_futures=True)

    async def test_auth_and_first_frame(self):
        async with ClientSession() as session:
            async with session.ws_connect(self.http.make_url("/ws")) as ws:
                reply = await authenticate(ws, "secret-123")
                self.assertEqual(reply["type"], "auth_ok")
                self.assertGreaterEqual(len(reply["monitors"]), 1)
                self.assertFalse(reply["tls"])
                self.assertEqual(reply["stream"]["codec"], "jpeg")
                self.assertEqual(reply["stream"]["quality_mode"], "fixed")

                monitor_id = reply["monitors"][0]["id"]
                await ws.send_json({"type": "view", "monitor": monitor_id})

                deadline = time.monotonic() + 10
                frame = None
                while time.monotonic() < deadline:
                    msg = await ws.receive(timeout=10)
                    if msg.type == WSMsgType.BINARY:
                        frame = msg.data
                        break
                    if msg.type == WSMsgType.TEXT:
                        self.assertNotIn('"error"', msg.data)
                self.assertIsNotNone(frame, "server did not send a frame")
                assert frame is not None
                header = unpack_header(frame)
                self.assertIsNotNone(header)
                assert header is not None
                self.assertEqual(header["monitor"], monitor_id)
                self.assertGreater(header["width"], 0)
                self.assertTrue(frame.endswith(b"\xff\xd9"))

                await ws.send_json({"type": "ping", "t": 123})
                pong = await receive_json(ws, 5)
                self.assertEqual(pong, {"type": "pong", "t": 123})

    async def test_live_stream_change(self):
        async with ClientSession() as session:
            async with session.ws_connect(self.http.make_url("/ws")) as ws:
                reply = await authenticate(ws, "secret-123")
                monitor_id = reply["monitors"][0]["id"]
                await ws.send_json({"type": "view", "monitor": monitor_id})

                deadline = time.monotonic() + 10
                first = None
                while time.monotonic() < deadline:
                    msg = await ws.receive(timeout=10)
                    if msg.type == WSMsgType.BINARY:
                        header = unpack_header(msg.data)
                        if header and header["codec"] == "audio":
                            continue
                        first = msg.data
                        break
                self.assertIsNotNone(first)
                assert first is not None
                self.assertEqual(unpack_header(first)["codec"], "jpeg")

                await ws.send_json(
                    {
                        "type": "stream",
                        "codec": "webp",
                        "quality_mode": "fixed",
                        "quality": 80,
                        "width": 640,
                    }
                )
                stream_ok = await receive_json(ws, 5)
                self.assertEqual(stream_ok["type"], "stream_ok")
                self.assertEqual(stream_ok["codec"], "webp")
                self.assertEqual(stream_ok["width"], 640)

                deadline = time.monotonic() + 10
                second = None
                while time.monotonic() < deadline:
                    msg = await ws.receive(timeout=10)
                    if msg.type == WSMsgType.BINARY:
                        header = unpack_header(msg.data)
                        if header and header["codec"] == "audio":
                            continue
                        second = msg.data
                        break
                self.assertIsNotNone(second)
                assert second is not None
                header = unpack_header(second)
                self.assertEqual(header["codec"], "webp")
                self.assertEqual(header["width"], 640)
                self.assertGreater(header["height"], 0)

    async def test_wrong_password(self):
        async with ClientSession() as session:
            async with session.ws_connect(self.http.make_url("/ws")) as ws:
                reply = await authenticate(ws, "wrong-password")
                self.assertEqual(reply["type"], "auth_fail")

    async def test_second_viewer_is_rejected(self):
        async with ClientSession() as session:
            async with session.ws_connect(self.http.make_url("/ws")) as first:
                first_reply = await authenticate(first, "secret-123")
                self.assertEqual(first_reply["type"], "auth_ok")
                async with session.ws_connect(self.http.make_url("/ws")) as second:
                    second_reply = await authenticate(second, "secret-123")
                    self.assertEqual(second_reply["type"], "busy")

    async def test_foreign_origin_is_rejected(self):
        async with ClientSession() as session:
            with self.assertRaises(WSServerHandshakeError) as caught:
                await session.ws_connect(
                    self.http.make_url("/ws"),
                    headers={"Origin": "https://evil.example"},
                )
            self.assertEqual(caught.exception.status, 403)

    async def test_security_headers_present(self):
        async with ClientSession() as session:
            async with session.get(self.http.make_url("/")) as response:
                await response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
