#!/usr/bin/env python3
"""Tiny binary WebSocket-to-TCP proxy for the local VNC server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HTTP_HEADER = 16_384
MAX_FRAME = 16 * 1024 * 1024
VNC_PATH_TOKEN = os.getenv("VNC_PATH_TOKEN", "").strip()


async def read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, dict[str, str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    if len(raw) > MAX_HTTP_HEADER:
        raise ValueError("HTTP headers are too large")
    lines = raw.decode("latin-1").split("\r\n")
    request_parts = lines[0].split()
    if len(request_parts) < 2:
        raise ValueError("invalid HTTP request line")
    path = request_parts[1].split("?", 1)[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return path, headers


async def handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> str:
    path, headers = await read_http_request(reader)
    key = headers.get("sec-websocket-key")
    if not key or headers.get("upgrade", "").lower() != "websocket":
        raise ValueError("not a WebSocket upgrade")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest())
    response = [
        b"HTTP/1.1 101 Switching Protocols",
        b"Upgrade: websocket",
        b"Connection: Upgrade",
        b"Sec-WebSocket-Accept: " + accept,
    ]
    protocols = {
        item.strip()
        for item in headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    }
    if "binary" in protocols:
        response.append(b"Sec-WebSocket-Protocol: binary")
    writer.write(b"\r\n".join(response) + b"\r\n\r\n")
    await writer.drain()
    return path


def authorized_channel(path: str) -> str | None:
    """Return the requested channel only when its optional path token matches."""
    pieces = [piece for piece in path.split("/") if piece]
    if VNC_PATH_TOKEN:
        if len(pieces) != 2 or not hmac.compare_digest(pieces[0], VNC_PATH_TOKEN):
            return None
        channel = pieces[1]
    else:
        if len(pieces) != 1:
            return None
        channel = pieces[0]
    return channel if channel in {"websockify", "audio"} else None


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > MAX_FRAME:
        raise ValueError("WebSocket frame is too large")
    masked = bool(second & 0x80)
    mask = await reader.readexactly(4) if masked else b""
    payload = bytearray(await reader.readexactly(length))
    if masked:
        for index in range(length):
            payload[index] ^= mask[index % 4]
    return opcode, bytes(payload)


async def send_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes) -> None:
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    writer.write(header + payload)
    await writer.drain()


async def websocket_to_tcp(
    websocket_reader: asyncio.StreamReader,
    websocket_writer: asyncio.StreamWriter,
    tcp_writer: asyncio.StreamWriter,
) -> None:
    while True:
        opcode, payload = await read_frame(websocket_reader)
        if opcode == 0x8:
            return
        if opcode == 0x9:
            await send_frame(websocket_writer, 0xA, payload)
            continue
        if opcode in {0x0, 0x1, 0x2}:
            tcp_writer.write(payload)
            await tcp_writer.drain()


async def tcp_to_websocket(
    tcp_reader: asyncio.StreamReader,
    websocket_writer: asyncio.StreamWriter,
) -> None:
    while payload := await tcp_reader.read(65_536):
        await send_frame(websocket_writer, 0x2, payload)


async def websocket_control(
    websocket_reader: asyncio.StreamReader,
    websocket_writer: asyncio.StreamWriter,
) -> None:
    while True:
        opcode, payload = await read_frame(websocket_reader)
        if opcode == 0x8:
            return
        if opcode == 0x9:
            await send_frame(websocket_writer, 0xA, payload)


async def pulse_to_websocket(websocket_writer: asyncio.StreamWriter) -> None:
    process = await asyncio.create_subprocess_exec(
        "parec",
        "--raw",
        "--device=mcp_output.monitor",
        "--format=s16le",
        "--rate=48000",
        "--channels=2",
        "--latency-msec=40",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        while payload := await process.stdout.read(3840):
            await send_frame(websocket_writer, 0x2, payload)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def audio_proxy(
    websocket_reader: asyncio.StreamReader,
    websocket_writer: asyncio.StreamWriter,
) -> None:
    tasks = {
        asyncio.create_task(websocket_control(websocket_reader, websocket_writer)),
        asyncio.create_task(pulse_to_websocket(websocket_writer)),
    }
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def proxy(
    websocket_reader: asyncio.StreamReader,
    websocket_writer: asyncio.StreamWriter,
) -> None:
    tcp_writer: asyncio.StreamWriter | None = None
    try:
        path = await handshake(websocket_reader, websocket_writer)
        channel = authorized_channel(path)
        if channel is None:
            raise ValueError("invalid websocket path")
        if channel == "audio":
            await audio_proxy(websocket_reader, websocket_writer)
            return
        tcp_reader, tcp_writer = await asyncio.open_connection("127.0.0.1", 5900)
        tasks = {
            asyncio.create_task(
                websocket_to_tcp(websocket_reader, websocket_writer, tcp_writer)
            ),
            asyncio.create_task(tcp_to_websocket(tcp_reader, websocket_writer)),
        }
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        if tcp_writer is not None:
            tcp_writer.close()
            try:
                await tcp_writer.wait_closed()
            except ConnectionError:
                pass
        websocket_writer.close()
        try:
            await websocket_writer.wait_closed()
        except ConnectionError:
            pass


async def main() -> None:
    server = await asyncio.start_server(proxy, "0.0.0.0", 6080)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
