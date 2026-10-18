"""A small WebSocket server side (RFC 6455), stdlib only.

Terminal I/O used to be one HTTP request per keystroke and a poll every
120 ms for output, through two hops (page -> UI server -> host). Typing and
pasting lagged visibly. A WebSocket from the page straight to the host carries
keystrokes in and output out over one open connection, and output is pushed
the moment the terminal produces it.

Only what a terminal needs: text frames, ping/pong, close, fragmented client
messages. No extensions, no compression, no binary payloads.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
from typing import Optional

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 4 * 1024 * 1024      # a paste larger than this is refused

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key.strip() + _GUID).encode()).digest()
    return base64.b64encode(digest).decode()


class Closed(Exception):
    pass


class WebSocket:
    """One accepted connection. Sends are thread-safe; receive from one thread."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    # -- sending ---------------------------------------------------------

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(n)
        elif n < 65536:
            head.append(126)
            head += struct.pack("!H", n)
        else:
            head.append(127)
            head += struct.pack("!Q", n)
        return bytes(head) + payload

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise Closed()
        frame = self._frame(opcode, payload)
        with self._send_lock:
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                self.closed = True
                raise Closed() from exc

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            self._send(OP_CLOSE, struct.pack("!H", code))
        except Closed:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- receiving -------------------------------------------------------

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except OSError as exc:
                raise Closed() from exc
            if not chunk:
                raise Closed()
            buf += chunk
        return bytes(buf)

    def recv_text(self) -> Optional[str]:
        """The next complete text message; None when the peer closes."""
        parts: list[bytes] = []
        size = 0
        while True:
            try:
                b1, b2 = self._read_exact(2)
            except Closed:
                self.closed = True
                return None
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, length = b2 & 0x80, b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if not masked or length > MAX_MESSAGE:
                # Clients must mask; an oversized frame is not a terminal's.
                self.close(1002 if not masked else 1009)
                return None
            mask = self._read_exact(4)
            raw = self._read_exact(length)
            # Unmask as one big integer XOR: a byte loop in Python took
            # seconds on a large paste.
            if length:
                key = (mask * (length // 4 + 1))[:length]
                data = (int.from_bytes(raw, "big") ^ int.from_bytes(key, "big")).to_bytes(length, "big")
            else:
                data = b""

            if opcode == OP_CLOSE:
                self.close()
                return None
            if opcode == OP_PING:
                try:
                    self._send(OP_PONG, bytes(data))
                except Closed:
                    return None
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BIN, OP_CONT):
                parts.append(bytes(data))
                size += length
                if size > MAX_MESSAGE:
                    self.close(1009)
                    return None
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")
                continue
            self.close(1002)
            return None
