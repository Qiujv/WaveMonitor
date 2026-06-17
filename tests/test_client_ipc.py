import time
import warnings

import pytest
from PySide6.QtNetwork import QLocalServer

import wave_monitor.client as client_module
from wave_monitor.client import WaveMonitor
from wave_monitor.ipc.launcher import can_connect_to_server, start_wave_monitor
from wave_monitor.ipc.state_memory import ClientStateMemory


def wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def send_ipc_probe(client: WaveMonitor, value: int) -> None:
    client._io.submit_write({"_type": "_set_ipc_probe_value", "value": value})


def close_real_server() -> None:
    client = WaveMonitor(create_window=False)
    try:
        client.close_window()
    finally:
        client.close()


@pytest.fixture
def real_server():
    if can_connect_to_server():
        pytest.skip("A WaveMonitor server is already running.")

    start_wave_monitor()
    assert wait_until(can_connect_to_server), "WaveMonitor server did not start."
    try:
        yield
    finally:
        if can_connect_to_server():
            close_real_server()
            assert wait_until(lambda: not can_connect_to_server())


def test_multiple_clients_can_send_to_one_server(real_server):
    state = ClientStateMemory()
    client1 = WaveMonitor(create_window=False)
    client2 = WaveMonitor(create_window=False)
    try:
        send_ipc_probe(client1, 11)
        assert wait_until(lambda: state.get_ipc_probe_value() == 11)

        send_ipc_probe(client2, 22)
        assert wait_until(lambda: state.get_ipc_probe_value() == 22)
    finally:
        client1.close()
        client2.close()
        state.close()


def test_client_warns_when_server_is_down(monkeypatch):
    name = "wm_server_down_test"
    monkeypatch.setattr(client_module, "PIPE_NAME", name)
    QLocalServer.removeServer(name)
    client = WaveMonitor(create_window=False)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client.add_note("wave", "server is down")
            assert wait_until(
                lambda: any("Not connected to server" in str(w.message) for w in caught),
                timeout_s=2.0,
            )
    finally:
        client.close()
        QLocalServer.removeServer(name)


def test_client_reconnects_after_server_restarts(real_server):
    client = WaveMonitor(create_window=False)
    try:
        send_ipc_probe(client, 1)
        assert wait_until(lambda: client._state_memory.get_ipc_probe_value() == 1)

        client.close_window()
        client.close()
        assert wait_until(lambda: not can_connect_to_server())

        start_wave_monitor()
        assert wait_until(can_connect_to_server), "WaveMonitor server did not restart."

        client = WaveMonitor(create_window=False)
        send_ipc_probe(client, 2)
        assert wait_until(lambda: client._state_memory.get_ipc_probe_value() == 2)
    finally:
        client.close()
