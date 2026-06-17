import numpy as np
import pytest
from PySide6.QtNetwork import QLocalServer

from wave_monitor.ipc.launcher import can_connect_to_server
from wave_monitor.ipc.messages import (
    MessageReader,
    encode,
    pack_message,
    write_message,
)


class _ReadResult:
    def __init__(self, data: bytes):
        self._data = data

    def data(self) -> bytes:
        return self._data


class FakeSocket:
    def __init__(self, incoming: bytes = b""):
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()
        self.wait_for_bytes_written_count = 0

    def bytesAvailable(self):
        return len(self.incoming)

    def read(self, size: int):
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return _ReadResult(data)

    def write(self, payload: bytes):
        self.outgoing.extend(payload)
        return len(payload)

    def waitForReadyRead(self, timeout_ms: int):
        return bool(self.incoming)

    def flush(self):
        return True

    def waitForBytesWritten(self):
        self.wait_for_bytes_written_count += 1
        return True


class FullyFlushedSocket(FakeSocket):
    def waitForBytesWritten(self):
        self.wait_for_bytes_written_count += 1
        return False

    def bytesToWrite(self):
        return 0


class PendingWriteSocket(FakeSocket):
    def waitForBytesWritten(self):
        self.wait_for_bytes_written_count += 1
        return False

    def bytesToWrite(self):
        return 1


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def test_write_message_succeeds_when_socket_is_already_flushed():
    sock = FullyFlushedSocket()

    write_message(sock, {"_type": "autoscale"})

    assert sock.outgoing
    assert sock.wait_for_bytes_written_count == 0


def test_write_message_times_out_when_socket_still_has_pending_bytes():
    sock = PendingWriteSocket()

    with pytest.raises(RuntimeError, match="Timed out while writing message"):
        write_message(sock, {"_type": "autoscale"})


def test_message_reader_preserves_numpy_arrays():
    msg = {"_type": "add_wfm", "name": "x", "t": np.arange(3), "ys": [np.ones(3)]}
    sock = FakeSocket()

    write_message(sock, msg)
    reader = MessageReader()
    decoded = reader.read_available(FakeSocket(bytes(sock.outgoing)))[0]

    assert decoded["_type"] == "add_wfm"
    np.testing.assert_array_equal(decoded["t"], msg["t"])
    np.testing.assert_array_equal(decoded["ys"][0], msg["ys"][0])


def test_message_reader_returns_all_complete_messages():
    reader = MessageReader()
    msg1 = {"_type": "autoscale"}
    msg2 = {"_type": "remove_wfm", "name": "wave"}
    sock = FakeSocket(_frame(encode(msg1)) + _frame(encode(msg2)))

    assert reader.read_available(sock) == [msg1, msg2]


def test_pack_message_adds_length_prefix():
    msg = {"_type": "add_note", "name": "wave", "note": "ok"}
    frame = pack_message(msg)

    payload_length = int.from_bytes(frame[:4], "big")
    assert payload_length == len(frame) - 4
    assert MessageReader().read_available(FakeSocket(frame)) == [msg]


def test_can_connect_to_server(qapp):
    name = "wm_probe_test"
    server = QLocalServer()
    try:
        assert not can_connect_to_server(name, timeout_ms=10)
        assert server.listen(name)
        assert can_connect_to_server(name, timeout_ms=100)
    finally:
        server.close()
        QLocalServer.removeServer(name)
