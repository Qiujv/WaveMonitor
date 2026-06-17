import warnings

import numpy as np
import pytest

from wave_monitor.client import WaveMonitor
from wave_monitor.ipc.state_memory import ClientStateMemory, ServerStateMemory


def test_add_wfm_type_and_shape_checks():
    wm = WaveMonitor(create_window=False)
    try:
        with pytest.raises(TypeError):
            wm.add_wfm(123, np.array([0, 1]), [np.array([0, 1])])
        with pytest.raises(TypeError):
            wm.add_wfm("n", [0, 1], [np.array([0, 1])])
        with pytest.raises(TypeError):
            wm.add_wfm("n", np.array([0, 1]), np.array([0, 1]))
        with pytest.raises(ValueError):
            wm.add_wfm("n", np.zeros((2, 2)), [np.zeros((2,))])
        with pytest.raises(TypeError):
            wm.add_wfm("n", np.array([0, 1]), ["bad"])
        with pytest.raises(ValueError):
            wm.add_wfm("n", np.array([0, 1]), [np.zeros((1, 1))])
        with pytest.raises(ValueError):
            wm.add_wfm("n", np.array([0, 1, 2]), [np.array([0, 1])])
    finally:
        wm.close()


def test_add_line_delegates_to_add_wfm(monkeypatch):
    wm = WaveMonitor(create_window=False)

    called = {}

    def fake_add_wfm(name, t, ys):
        called["args"] = (name, t.copy(), [y.copy() for y in ys])

    try:
        monkeypatch.setattr(wm, "add_wfm", fake_add_wfm)

        t = np.array([0, 1])
        ys = [np.array([0, 1])]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wm.add_line("n", t, ys, offset=0)

        assert "args" in called
        assert called["args"][0] == "n"
        np.testing.assert_array_equal(called["args"][1], t)
        np.testing.assert_array_equal(called["args"][2][0], ys[0])
    finally:
        wm.close()


def test_get_wfm_interval_from_state_memory():
    test_interval = 2.5
    memory_name = "wm_test_interval"
    server_state = ServerStateMemory(name=memory_name)
    wm = WaveMonitor(create_window=False)
    wm._state_memory.close()
    wm._state_memory = ClientStateMemory(name=memory_name)
    try:
        server_state.wfm_interval = test_interval
        assert wm.get_wfm_interval() == pytest.approx(test_interval)

        wm._state_memory.close()
        server_state.close()
        assert wm.get_wfm_interval() == 0.0
    finally:
        wm.close()
        server_state.close()


def test_client_state_memory_falls_back_when_server_state_is_missing(caplog):
    state = ClientStateMemory(name="wm_missing_state")
    try:
        assert state.get_wfm_interval() == 0.0
        assert "Failed to read wfm_interval" not in caplog.text
    finally:
        state.close()

