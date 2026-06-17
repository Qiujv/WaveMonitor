from PySide6.QtCore import QObject

import wave_monitor.window as window_module
from wave_monitor.ipc.messages import MessageReader, encode
from wave_monitor.window import DataSource


class _ReadResult:
    def __init__(self, data: bytes):
        self._data = data

    def data(self) -> bytes:
        return self._data


class FakeSocket:
    def __init__(self, payload: bytes):
        self._buffer = bytearray(payload)
        self.closed = False

    def bytesAvailable(self):
        return len(self._buffer)

    def read(self, size: int):
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return _ReadResult(data)

    class _Signal:
        def disconnect(self, *args, **kwargs):
            return None

    @property
    def readyRead(self):
        return FakeSocket._Signal()

    @property
    def disconnected(self):
        return FakeSocket._Signal()

    def close(self):
        self.closed = True


def _frame(msg: dict) -> bytes:
    payload = encode(msg)
    return len(payload).to_bytes(4, "big") + payload


def test_data_source_decodes_framed_msgpack_messages(qapp):
    parent = QObject()
    server = DataSource(parent)
    received = []
    server.add_note.connect(lambda name, note: received.append((name, note)))
    sock = FakeSocket(_frame({"_type": "add_note", "name": "wave", "note": "ok"}))
    server._client_readers[sock] = MessageReader()

    try:
        server.read_frame(sock)

        assert received == [("wave", "ok")]
    finally:
        server.close()


def test_data_source_closes_each_client_connection(qapp):
    parent = QObject()
    server = DataSource(parent)
    sock1 = FakeSocket(b"")
    sock2 = FakeSocket(b"")
    server._client_readers[sock1] = MessageReader()
    server._client_readers[sock2] = MessageReader()

    try:
        server.close_client_connection(sock1)

        assert sock1.closed
        assert sock1 not in server._client_readers
        assert sock2 in server._client_readers
    finally:
        server.close()


def test_data_source_logs_client_id_on_disconnect(qapp, caplog):
    parent = QObject()
    server = DataSource(parent)
    sock = FakeSocket(b"")
    server._client_readers[sock] = MessageReader()
    server._client_ids[sock] = 7

    try:
        with caplog.at_level("INFO", logger=window_module.logger.name):
            server.close_client_connection(sock)

        assert "Client 7 disconnected." in caplog.text
    finally:
        server.close()
