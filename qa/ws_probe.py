#!/usr/bin/env python3
from __future__ import annotations

import base64
import os
import socket
import struct
import sys
from urllib.parse import urlparse


def receive_exact(sock: socket.socket, count: int) -> bytes:
    parts: list[bytes] = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("WebSocket closed early")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: ws_probe.py http://127.0.0.1:PORT/handoff.html")
    parsed = urlparse(sys.argv[1])
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise SystemExit("probe accepts a loopback URL with an explicit port")
    path = parsed.path.rsplit("/", 1)[0] + "/websockify"
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: binary\r\n\r\n"
    ).encode()
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(request)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += sock.recv(4096)
        headers, buffer = buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in headers.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {headers[:120]!r}")
        while len(buffer) < 2:
            buffer += sock.recv(4096)
        first, second = buffer[0], buffer[1]
        buffer = buffer[2:]
        if first & 0x0F != 2:
            raise RuntimeError(
                f"expected binary WebSocket frame, opcode={first & 0x0F}"
            )
        length = second & 0x7F
        if length == 126:
            while len(buffer) < 2:
                buffer += sock.recv(4096)
            length = struct.unpack("!H", buffer[:2])[0]
            buffer = buffer[2:]
        elif length == 127:
            while len(buffer) < 8:
                buffer += sock.recv(4096)
            length = struct.unpack("!Q", buffer[:8])[0]
            buffer = buffer[8:]
        while len(buffer) < length:
            buffer += sock.recv(4096)
        payload = buffer[:length]
        if not payload.startswith(b"RFB "):
            raise RuntimeError(f"expected RFB greeting, received {payload[:20]!r}")
    print(payload.decode("ascii", "replace").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
