import multiprocessing.shared_memory as shared_memory
import time

from wave_monitor.client import WaveMonitor
from wave_monitor.constants import STATE_MEMORY_NAME
from wave_monitor.ipc.state_memory import ClientStateMemory, ServerStateMemory
from wave_monitor.window import MonitorWindow


def test_server_client_shared_memory_integration(qapp):
    monitor = MonitorWindow()
    client = WaveMonitor(create_window=False)
    try:
        # Initial default
        assert (
            abs(client.get_wfm_interval() - ServerStateMemory.DEFAULT_WFM_INTERVAL)
            < 1e-6
        )

        for val in (0.5, 1.2, 0.8):
            monitor.state.wfm_interval = val
            # allow a tiny delay for client to read new value (though direct read)
            time.sleep(0.05)
            assert abs(client.get_wfm_interval() - val) < 1e-6
    finally:
        client.close()
        monitor.window.close()


def test_wfm_interval_property(qapp):
    mw = MonitorWindow()
    try:
        # Read default value via state
        default_val = mw.state.wfm_interval
        assert isinstance(default_val, float)

        # Update via state property
        mw.state.wfm_interval = 0.7
        assert abs(mw.state.wfm_interval - 0.7) < 1e-6

        # Read underlying shared memory directly
        shm = shared_memory.ShareableList(name=STATE_MEMORY_NAME)
        try:
            assert abs(float(shm[0]) - 0.7) < 1e-6
        finally:
            shm.shm.close()
    finally:
        mw.window.close()


def test_ipc_probe_value_property():
    memory_name = "wm_ipc_probe_state"
    server_state = ServerStateMemory(name=memory_name)
    try:
        client_state = ClientStateMemory(name=memory_name)
        try:
            assert client_state.get_ipc_probe_value() == 0
            server_state.ipc_probe_value = 42
            assert client_state.get_ipc_probe_value() == 42
        finally:
            client_state.close()
    finally:
        server_state.close()


def test_server_state_memory_cleans_stale_memory():
    stale = shared_memory.ShareableList([9.9], name=STATE_MEMORY_NAME)
    stale.shm.close()
    state = ServerStateMemory()
    try:
        assert state.wfm_interval == ServerStateMemory.DEFAULT_WFM_INTERVAL
    finally:
        state.close()


def test_client_close_does_not_unlink_server_state_memory():
    memory_name = "wm_client_close_state"
    server_state = ServerStateMemory(name=memory_name)
    try:
        server_state.wfm_interval = 1.7
        client_state = ClientStateMemory(name=memory_name)
        assert abs(client_state.get_wfm_interval() - 1.7) < 1e-6
        client_state.close()

        next_client_state = ClientStateMemory(name=memory_name)
        try:
            assert abs(next_client_state.get_wfm_interval() - 1.7) < 1e-6
        finally:
            next_client_state.close()
    finally:
        server_state.close()


def test_wfm_interval_update_reflected_in_shared_memory(qapp):
    """Setting state.wfm_interval should reflect in shared memory list."""
    mw = MonitorWindow()
    try:
        mw.state.wfm_interval = 1.1
        shm = shared_memory.ShareableList(name=STATE_MEMORY_NAME)
        try:
            assert abs(float(shm[0]) - 1.1) < 1e-6
        finally:
            shm.shm.close()
    finally:
        mw.window.close()
