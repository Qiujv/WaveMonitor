"""Small msgpack helpers for WaveMonitor's local socket protocol.

Messages are sent as:

    4-byte big-endian payload length + msgpack payload

On POSIX systems, Qt's QLocalServer listens on a Unix domain socket path.
That means a Python ``socket.AF_UNIX`` client can talk to the Qt server as
long as it sends the same bytes. WaveMonitor uses that path on macOS because
QLocalSocket writes from a regular Python thread can leave large frames stuck
in Qt's pending-write buffer until the socket closes.
"""

from __future__ import annotations

import os
import socket
import tempfile

import msgpack
import msgpack_numpy
from PySide6.QtNetwork import QLocalSocket

from wave_monitor.constants import CHUNK_SIZE, HEAD_LENGTH


def encode(obj) -> bytes:
    return msgpack.packb(obj, default=msgpack_numpy.encode, use_bin_type=True)


def decode(data: bytes):
    return msgpack.unpackb(data, object_hook=msgpack_numpy.decode, raw=False)


def pack_message(msg: dict) -> bytes:
    """Return one length-prefixed msgpack frame."""
    payload = encode(msg)
    return len(payload).to_bytes(HEAD_LENGTH, "big") + payload


def write_message(sock: QLocalSocket, msg: dict) -> None:
    """Write one message through a QLocalSocket."""
    frame = pack_message(msg)
    for start in range(0, len(frame), CHUNK_SIZE):
        chunk = frame[start : start + CHUNK_SIZE]
        written_total = 0
        while written_total < len(chunk):
            written = sock.write(chunk[written_total:])
            if written == -1:
                raise RuntimeError("Failed to write message to socket.")
            if written == 0:
                raise RuntimeError("Socket wrote 0 bytes while writing message.")

            written_total += written
            sock.flush()
            if _bytes_to_write(sock) == 0:
                continue
            if not sock.waitForBytesWritten() and _bytes_to_write(sock) != 0:
                raise RuntimeError("Timed out while writing message to socket.")


def write_native_message(server_name: str, msg: dict) -> None:
    """Write one message to a QLocalServer without using QLocalSocket."""
    frame = pack_message(msg)
    if os.name == "nt":
        _write_windows_named_pipe(server_name, frame)
        return

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(local_server_path(server_name))
        sock.sendall(frame)


def _write_windows_named_pipe(server_name: str, frame: bytes) -> None:
    path = local_server_path(server_name)
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        view = memoryview(frame)
        written_total = 0
        while written_total < len(view):
            written = os.write(fd, view[written_total:])
            if written == 0:
                raise RuntimeError("Named pipe wrote 0 bytes while writing message.")
            written_total += written
    finally:
        os.close(fd)


class MessageReader:
    """Collect available bytes from one client socket and return complete messages."""

    def __init__(self):
        self._buffer = bytearray()

    def read_available(self, sock: QLocalSocket) -> list[dict | None]:
        while sock.bytesAvailable():
            size = min(sock.bytesAvailable(), CHUNK_SIZE)
            self._buffer.extend(sock.read(size).data())

        messages = []
        while len(self._buffer) >= HEAD_LENGTH:
            payload_length = int.from_bytes(self._buffer[:HEAD_LENGTH], "big")
            frame_length = HEAD_LENGTH + payload_length
            if len(self._buffer) < frame_length:
                break

            payload = bytes(self._buffer[HEAD_LENGTH:frame_length])
            del self._buffer[:frame_length]
            try:
                messages.append(decode(payload))
            except Exception:
                messages.append(None)
        return messages


def _bytes_to_write(sock: QLocalSocket) -> int | None:
    method = getattr(sock, "bytesToWrite", None)
    if not callable(method):
        return None
    return int(method())


def local_server_path(name: str) -> str:
    """Return the Unix socket path used by QLocalServer for a server name."""
    if os.name == "nt":
        if name.startswith("\\\\.\\pipe\\"):
            return name
        return f"\\\\.\\pipe\\{name}"
    if os.path.sep in name:
        return name
    return os.path.join(tempfile.gettempdir(), name)
