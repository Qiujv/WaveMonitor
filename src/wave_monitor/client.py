import logging
import os
import queue
import threading
import time
import warnings

import numpy as np
from PySide6.QtNetwork import QLocalSocket
from typing_extensions import deprecated

from .constants import PIPE_NAME
from .ipc.launcher import can_connect_to_server, start_wave_monitor
from .ipc.messages import write_message, write_native_message
from .ipc.state_memory import ClientStateMemory

logger = logging.getLogger(__name__)


class WaveMonitor:
    """Wrapper to operate Monitor in a separate process.

    A monitor window is created by
    either calling `find_or_create_window()`
    or run `start-wave-monitor` in a separate process.

    Note:
        The wrapper is not intend for Qt application, which means neither event loop,
        no receiving/emiting signals or slots.
    """

    logger = logger.getChild("WaveMonitor")

    def __init__(self, create_window: bool = True) -> None:
        # Background I/O worker
        self._io = _IOWorker()
        self._io.start()

        self._last_wfm_time = {}
        self._state_memory = ClientStateMemory()

        if create_window:
            try:
                self.find_or_create_window()
            except Exception:
                self.logger.exception(
                    "Failed to connect to server. Try `find_or_create_window()` later."
                )

    @deprecated("offset will be ignored. Use add_wfm instead.")
    def add_line(self, name: str, t: np.ndarray, ys: list[np.ndarray], offset) -> None:
        self.add_wfm(name, t, ys)

    def add_wfm(self, name: str, t: np.ndarray, ys: list[np.ndarray]) -> None:
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        if not isinstance(t, np.ndarray):
            raise TypeError("t must be a numpy array")
        if not isinstance(ys, list):
            raise TypeError("ys must be a list")
        if t.ndim != 1:
            raise ValueError("t must be 1D")
        for y in ys:
            if not isinstance(y, np.ndarray):
                raise TypeError("ys must be a list of numpy arrays")
            if y.ndim != 1:
                raise ValueError("ys must be a list of 1D numpy arrays")
            if y.shape != t.shape:
                raise ValueError("ys must have the same shape as t")

        now = time.time()
        server_interval = self.get_wfm_interval()
        last_time = self._last_wfm_time.get(name, 0)
        if (now - last_time) < server_interval:
            self.logger.info(
                "Skipping adding waveform '%s' due to interval limit: %.3f seconds.",
                name,
                server_interval,
            )
            return None
        self._last_wfm_time[name] = now

        self.logger.debug("Adding waveform '%s'", name)
        self._io.submit_write(dict(_type="add_wfm", name=name, t=t, ys=ys))

    def remove_wfm(self, name: str) -> None:
        if not isinstance(name, str):
            raise TypeError("name must be a string")

        self._io.submit_write(dict(_type="remove_wfm", name=name))

    def clear(self) -> None:
        """Set all waveforms to zero.

        Note: This does not remove the waveforms, right click on the window to remove them.
        """
        self._io.submit_write(dict(_type="clear"))

    def autoscale(self) -> None:
        self._io.submit_write(dict(_type="autoscale"))

    def add_note(self, name: str, note: str):
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        if not isinstance(note, str):
            raise TypeError("note must be a string")

        self._io.submit_write(dict(_type="add_note", name=name, note=note))

    def get_wfm_interval(self) -> float:
        return self._state_memory.get_wfm_interval()

    def close(self, immediate: bool = False, timeout: float | None = 1.0) -> None:
        """Wait all jobs done and close connection.
        
        Abort the jobs with `immediate=True`
        """
        try:
            self._io.stop(immediate=immediate)
        except Exception:
            pass
        try:
            self._io.join(timeout)
        except Exception:
            pass
        self._state_memory.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def find_or_create_window(self, timeout_s: float = 10) -> None:
        """Connect to existing monitor window or create one in new process.

        Blocks until connected to server.
        """
        if can_connect_to_server(PIPE_NAME, 100):
            return None

        start_wave_monitor()

        start_time = time.time()
        while not can_connect_to_server(PIPE_NAME, 100):
            self.logger.debug("Waiting for server to start listening.")
            if time.time() - start_time > timeout_s:
                raise TimeoutError("Timeout waiting for server to start listening.")
            time.sleep(0.1)

    def close_window(self) -> None:
        self._io.submit_write(dict(_type="close_window"))


class _IOWorker(threading.Thread):
    """A background I/O worker that owns the QLocalSocket and performs I/O."""

    logger = logger.getChild("IOWorker")

    def __init__(self):
        super().__init__(name="WaveMonitorIO", daemon=True)
        self._tasks: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()
        self._sock: QLocalSocket | None = None

    # Public API (thread-safe)
    def submit_write(self, msg: dict) -> None:
        self._submit("write", {"msg": msg})

    def _submit(self, op: str, payload: dict) -> None:
        try:
            self._tasks.put_nowait((op, payload))
        except queue.Full:
            message = f"I/O queue is full; dropping message: {op}"
            self.logger.warning(message)
            warnings.warn(message, stacklevel=3)

    def stop(self, immediate: bool = False) -> None:
        self._submit("_stop", {})
        if immediate:
            self._stop_event.set()

    # Thread run-loop
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                op, payload = self._tasks.get(timeout=0.1)
            except queue.Empty:
                continue

            if op == "_stop":
                break

            try:
                if op == "write":
                    self._handle_write(payload["msg"])  # fire-and-forget
                else:
                    self.logger.warning("Unknown op: %s", op)
            except Exception:
                self.logger.exception("IO worker op failed: %s", op)

        # Cleanup
        try:
            if self._sock is not None:
                if self._sock.state() == QLocalSocket.ConnectedState:
                    self._sock.disconnectFromServer()
                if self._sock.state() == QLocalSocket.ClosingState:
                    self._sock.waitForDisconnected()
        except Exception:
            pass

    # Internal helpers (run in worker thread)
    def _ensure_connected(self, timeout_ms: int = 100) -> bool:
        if self._sock is None:
            # Create socket in this thread to keep Qt thread affinity consistent
            self._sock = QLocalSocket()
        if self._sock.state() == QLocalSocket.ConnectedState:
            return True
        # Always disconnect first to refresh state
        try:
            self._sock.disconnectFromServer()
        except Exception:
            pass
        self._sock.connectToServer(PIPE_NAME)
        return bool(self._sock.waitForConnected(timeout_ms))


    def _handle_write(self, msg: dict) -> None:
        if os.name != "nt":
            try:
                write_native_message(PIPE_NAME, msg)
                return
            except OSError:
                warnings.warn("Not connected to server.", stacklevel=2)
                return

        try:
            if not self._ensure_connected(timeout_ms=100):
                warnings.warn("Not connected to server.", stacklevel=2)
            write_message(self._sock, msg)
        except RuntimeError:
            try:
                write_native_message(PIPE_NAME, msg)
            except OSError:
                warnings.warn("Not connected to server.", stacklevel=2)
