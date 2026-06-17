"""Minimal probes for QLocalSocket write latency on macOS.

Run examples:

    uv run python notebooks/probe_ipc_delay.py qt-small
    uv run python notebooks/probe_ipc_delay.py qt-large
    uv run python notebooks/probe_ipc_delay.py native-large

The important comparison is:

- qt-large: writes a large frame with QLocalSocket from a Python thread.
- native-large: writes the same frame with a POSIX Unix domain socket.

On macOS, QLocalSocket can leave a large pending write buffered until the
socket is closed. The native socket path sends the same length-prefixed
msgpack frame immediately to the Qt QLocalServer.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

import numpy as np
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from wave_monitor.ipc.messages import MessageReader, encode, write_message


PIPE_NAME = "wm_ipc_delay_probe"


class ProbeServer:
    def __init__(self, name: str):
        self.name = name
        self.app = QCoreApplication(sys.argv)
        self.server = QLocalServer()
        self.readers: dict[QLocalSocket, MessageReader] = {}
        self.start = time.monotonic()

        QLocalServer.removeServer(name)
        self.server.newConnection.connect(self._handle_new_connection)
        if not self.server.listen(name):
            raise RuntimeError(self.server.errorString())

        self.poller = QTimer()
        self.poller.setInterval(20)
        self.poller.timeout.connect(self._poll_clients)
        self.poller.start()

    @property
    def path(self) -> str:
        return self.server.fullServerName()

    def log(self, *parts) -> None:
        print(f"{time.monotonic() - self.start:6.3f}s", *parts, flush=True)

    def run(self, timeout_ms: int = 5000) -> None:
        QTimer.singleShot(timeout_ms, self.app.quit)
        self.app.exec()
        self.server.close()
        QLocalServer.removeServer(self.name)

    def _handle_new_connection(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self.readers[sock] = MessageReader()
            sock_id = id(sock)
            self.log("server accepted", sock_id)
            sock.readyRead.connect(lambda s=sock: self._read_available(s))
            sock.disconnected.connect(lambda sid=sock_id: self.log("server disconnected", sid))

    def _poll_clients(self) -> None:
        for sock in list(self.readers):
            self._read_available(sock)

    def _read_available(self, sock: QLocalSocket) -> None:
        reader = self.readers.get(sock)
        if reader is None:
            return
        for msg in reader.read_available(sock):
            if msg is None:
                self.log("server failed to decode a frame")
                continue
            self.log("server received", summarize_message(msg))
            self.app.quit()


def summarize_message(msg: dict) -> dict:
    summary = dict(msg)
    if "t" in summary:
        summary["t"] = f"array(len={len(summary['t'])})"
    if "ys" in summary:
        summary["ys"] = [f"array(len={len(y)})" for y in summary["ys"]]
    return summary


def make_message(kind: str) -> dict:
    if kind == "small":
        return {"_type": "probe", "i": 1}

    t = np.linspace(0, 1, 1_000_001)
    y = np.sin(t)
    return {"_type": "add_wfm", "name": "big", "t": t, "ys": [y, y]}


def make_frame(msg: dict) -> bytes:
    payload = encode(msg)
    return len(payload).to_bytes(4, "big") + payload


def send_with_qt_socket(name: str, msg: dict, wait_for_bytes: bool) -> None:
    sock = QLocalSocket()
    sock.connectToServer(name)
    print("client connected?", sock.waitForConnected(1000), flush=True)

    if wait_for_bytes:
        write_message(sock, msg)
    else:
        frame = make_frame(msg)
        written = sock.write(frame)
        print("client write returned", written, "bytesToWrite", sock.bytesToWrite())
        sock.flush()

    time.sleep(2)
    sock.disconnectFromServer()


def send_with_native_socket(path: str, msg: dict) -> None:
    frame = make_frame(msg)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)
        print("native client sending", len(frame), "bytes", flush=True)
        sock.sendall(frame)
        print("native client sent all", flush=True)
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=["qt-small", "qt-large", "native-large"],
        help="Which client write path to probe.",
    )
    args = parser.parse_args()

    server = ProbeServer(PIPE_NAME)
    server.log("server listening", server.path)

    if args.mode == "qt-small":
        def target() -> None:
            send_with_qt_socket(PIPE_NAME, make_message("small"), True)
    elif args.mode == "qt-large":
        def target() -> None:
            send_with_qt_socket(PIPE_NAME, make_message("large"), False)
    else:
        def target() -> None:
            send_with_native_socket(server.path, make_message("large"))

    threading.Thread(target=target, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
