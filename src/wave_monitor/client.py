import logging
import multiprocessing.shared_memory as shared_memory
import platform
import queue
import subprocess
import threading
import time
import warnings
from concurrent.futures import Future
from typing import Literal

import numpy as np
from PySide6.QtNetwork import QLocalSocket
from typing_extensions import deprecated

from .constants import CHUNK_SIZE, HEAD_LENGTH, PIPE_NAME, SHARED_MEMORY_NAME
from .proto import encode

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
        self._shared_memory = None  # Will be initialized when needed

        if create_window:
            try:
                self.find_or_create_window()
            except Exception:
                self.logger.exception(
                    "Failed to connect to server. Try `find_or_create_window()` or `connect()` later."
                )

    @deprecated("offset will be ignored. Use add_wfm instead.")
    def add_line(self, name: str, t: np.ndarray, ys: list[np.ndarray], offset) -> None:
        self.add_wfm(name, t, ys)

    def add_wfm(
        self,
        name: str,
        t: np.ndarray,
        ys: list[np.ndarray],
        dtype: np.float32 | None = np.float32,
    ) -> None:
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
        self.write(dict(_type="add_wfm", name=name, t=t, ys=ys, _dtype=dtype))

    def get_wfm_interval(self) -> float:
        try:
            if self._shared_memory is None:
                self._shared_memory = shared_memory.ShareableList(
                    name=SHARED_MEMORY_NAME
                )

            return float(self._shared_memory[0])
        except Exception:
            self.logger.exception(
                "Failed to read wfm_interval from server, using fallback"
            )
            return 0.0

    def remove_wfm(self, name: str) -> None:
        if not isinstance(name, str):
            raise TypeError("name must be a string")

        self.write(dict(_type="remove_wfm", name=name))

    def clear(self) -> None:
        """Set all waveforms to zero.

        Note: This does not remove the waveforms, right click on the window to remove them.
        """
        self.write(dict(_type="clear"))

    def autoscale(self) -> None:
        self.write(dict(_type="autoscale"))

    def add_note(self, name: str, note: str):
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        if not isinstance(note, str):
            raise TypeError("note must be a string")

        self.write(dict(_type="add_note", name=name, note=note))

    def write(self, msg: dict) -> None:
        """Asynchronously send a message via the background I/O thread.

        Returns immediately. Errors (like not connected) will be logged/warned
        by the background worker.
        """
        self._io.submit_write(msg)

    def disconnect(self) -> None:
        fut = self._io.submit_disconnect()
        ok = fut.result(timeout=1.0)
        if not ok:
            raise RuntimeError("Could not disconnect from server")

    def close(self, drain: bool = True, timeout: float | None = 1.0) -> None:
        """Stop background I/O worker."""
        try:
            self._io.stop(drain=drain)
        except Exception:
            pass
        try:
            self._io.join(timeout)
        except Exception:
            pass
        # Clean up shared memory connection (don't unlink, server owns it)
        if self._shared_memory is not None:
            try:
                self._shared_memory.shm.close()
                self._shared_memory = None
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def connect(self, timeout_s: float | None = 0.1) -> bool:
        """Connect to server via the background worker and return success status."""
        fut = self._io.submit_connect(timeout_s)
        return bool(fut.result(timeout=max(1.0, timeout_s + 0.1)))

    def find_or_create_window(
        self,
        log_level: Literal["WARNING", "INFO", "DEBUG"] = "INFO",
        timeout_s: float = 10,
    ) -> None:
        """Connect to existing monitor window or create one in new process.

        Blocks until connected to server.
        """
        if not self.connect(timeout_s=0.1):
            start_wave_monitor(log_level)

        start_time = time.time()
        while not self.connect(timeout_s=0.1):
            self.logger.debug("Waiting for server to start listening.")
            if time.time() - start_time > timeout_s:
                raise TimeoutError("Timeout waiting for server to start listening.")
            time.sleep(0.1)


def start_wave_monitor(log_level: Literal["WARNING", "INFO", "DEBUG"] = "INFO"):
    system = platform.system()
    if system == "Windows":
        full_cmd = [
            "cmd",
            "/c",
            "start",
            "",
            "/B",
            "start-wave-monitor",
            f"--log={log_level}",
        ]
    else:
        full_cmd = ["start-wave-monitor", f"--log={log_level}"]
    subprocess.Popen(full_cmd, shell=True, close_fds=True)


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
        self._tasks.put(("write", {"msg": msg}))

    def submit_connect(self, timeout_ms: int) -> Future:
        fut: Future = Future()
        self._tasks.put(("connect", {"timeout_ms": timeout_ms, "future": fut}))
        return fut

    def submit_disconnect(self) -> Future:
        fut: Future = Future()
        self._tasks.put(("disconnect", {"future": fut}))
        return fut

    def stop(self, drain: bool = True) -> None:
        self._tasks.put(("_stop", {}))
        if not drain:
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
                elif op == "connect":
                    self._handle_connect(payload["timeout_ms"], payload["future"])
                elif op == "disconnect":
                    self._handle_disconnect(payload["future"])
                else:
                    self.logger.warning("Unknown op: %s", op)
            except Exception as e:
                self.logger.exception("IO worker op failed: %s", op)
                fut = payload.get("future")
                if isinstance(fut, Future) and not fut.done():
                    fut.set_exception(e)

        # Cleanup
        try:
            if self._sock is not None:
                if self._sock.state() == QLocalSocket.ConnectedState:
                    self._sock.disconnectFromServer()
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

    def _handle_connect(self, timeout_ms: int, fut: Future) -> None:
        ok = self._ensure_connected(timeout_ms)
        if not fut.done():
            fut.set_result(bool(ok))

    def _handle_disconnect(self, fut: Future) -> None:
        if self._sock is None:
            ok = True
        else:
            try:
                self._sock.disconnectFromServer()
                ok = True
                if self._sock.state() == QLocalSocket.ConnectedState:
                    ok = bool(self._sock.waitForDisconnected())
            except Exception:
                ok = False
        if not fut.done():
            fut.set_result(ok)

    def _write_payload(self, payload: bytes) -> None:
        assert self._sock is not None
        total_length = len(payload)
        self._sock.write(total_length.to_bytes(HEAD_LENGTH, "big"))
        self._sock.waitForBytesWritten()

        start = 0
        while start < total_length:
            end = min(start + CHUNK_SIZE, total_length)
            chunk = payload[start:end]
            written_len = self._sock.write(chunk)
            if written_len == -1:
                raise RuntimeError("Failed to write to socket.")
            start += written_len
            self.logger.debug(
                "Wrote %d bytes, %d bytes remaining.",
                written_len,
                total_length - start,
            )

    def _handle_write(self, msg: dict) -> None:
        if not self._ensure_connected(timeout_ms=100):
            warnings.warn("Not connected to server.", stacklevel=2)
            return

        try:
            if msg.get("_type") == "add_wfm":
                _dtype = msg.pop("_dtype", None)
                if _dtype is not None:
                    msg["ys"] = [
                        y.astype(_dtype) if y.dtype != _dtype else y
                        for y in msg.get("ys")
                    ]
        except Exception:
            self.logger.exception(
                "Failed dtype conversion in I/O worker; sending original arrays."
            )

        self.logger.debug("msg to send (async): %r", msg)
        payload = encode(msg)
        self._write_payload(payload)
