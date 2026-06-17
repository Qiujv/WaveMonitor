"""Small shared state values exchanged between client and server."""

from __future__ import annotations

import logging
import struct
import time
from multiprocessing import shared_memory

from wave_monitor.constants import STATE_MEMORY_NAME

logger = logging.getLogger(__name__)

DEFAULT_WFM_INTERVAL = 0.2
DEFAULT_IPC_PROBE_VALUE = 0


class ServerStateMemory:
    """Server-owned shared state backed by a ShareableList."""

    DEFAULT_WFM_INTERVAL = DEFAULT_WFM_INTERVAL
    DEFAULT_IPC_PROBE_VALUE = DEFAULT_IPC_PROBE_VALUE

    def __init__(self, name: str = STATE_MEMORY_NAME):
        self.name = name
        self._shared_memory: shared_memory.ShareableList | None = None
        self.create()

    def create(self) -> None:
        try:
            old_shm = shared_memory.ShareableList(name=self.name)
            old_shm.shm.close()
            old_shm.shm.unlink()
            logger.info("Removed stale shared state memory %r", self.name)
        except FileNotFoundError:
            pass

        try:
            self._shared_memory = shared_memory.ShareableList(
                [self.DEFAULT_WFM_INTERVAL, self.DEFAULT_IPC_PROBE_VALUE],
                name=self.name,
            )
            logger.info("Created shared state memory %r", self.name)
        except Exception:
            logger.exception("Failed to create shared state memory")

    def close(self, unlink: bool = True) -> None:
        if self._shared_memory is None:
            return
        try:
            self._shared_memory.shm.close()
            if unlink:
                self._shared_memory.shm.unlink()
            logger.info("Cleaned up shared state memory")
        except Exception:
            logger.exception("Error cleaning up shared state memory")
        finally:
            self._shared_memory = None

    @property
    def wfm_interval(self) -> float:
        if self._shared_memory is not None:
            try:
                return float(self._shared_memory[0])
            except Exception:
                logger.exception("Failed to read wfm_interval from shared state memory")
        return self.DEFAULT_WFM_INTERVAL

    @wfm_interval.setter
    def wfm_interval(self, value: float) -> None:
        if self._shared_memory is not None:
            try:
                self._shared_memory[0] = value
            except Exception:
                logger.exception("Failed to write wfm_interval to shared state memory")

    @property
    def ipc_probe_value(self) -> int:
        if self._shared_memory is not None:
            try:
                return int(self._shared_memory[1])
            except Exception:
                logger.exception("Failed to read ipc_probe_value from shared state memory")
        return self.DEFAULT_IPC_PROBE_VALUE

    @ipc_probe_value.setter
    def ipc_probe_value(self, value: int) -> None:
        if self._shared_memory is not None:
            try:
                self._shared_memory[1] = int(value)
            except Exception:
                logger.exception("Failed to write ipc_probe_value to shared state memory")


class ClientStateMemory:
    """Client-side attachment to server-owned shared state."""

    def __init__(self, name: str = STATE_MEMORY_NAME):
        self.name = name
        self._shared_memory: shared_memory.SharedMemory | None = None

    def get_wfm_interval(self) -> float:
        try:
            if self._shared_memory is None:
                self._attach()
            return _read_shareable_list_float(self._shared_memory, 0)
        except FileNotFoundError:
            logger.debug("Shared state memory %r is not available", self.name)
            return 0.0
        except Exception:
            logger.exception("Failed to read wfm_interval from shared state memory")
            return 0.0

    def get_ipc_probe_value(self) -> int:
        try:
            if self._shared_memory is None:
                self._attach()
            return _read_shareable_list_int(self._shared_memory, 1)
        except FileNotFoundError:
            logger.debug("Shared state memory %r is not available", self.name)
            return self.DEFAULT_IPC_PROBE_VALUE
        except Exception:
            logger.exception("Failed to read ipc_probe_value from shared state memory")
            return self.DEFAULT_IPC_PROBE_VALUE

    def wait_until_available(self, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if self._shared_memory is None:
                    self._attach()
                return True
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            except Exception:
                logger.exception("Failed to attach shared state memory")
                return False

    def close(self) -> None:
        if self._shared_memory is None:
            return
        try:
            self._shared_memory.close()
        except Exception:
            logger.exception("Failed to close shared state memory")
        finally:
            self._shared_memory = None

    def _attach(self) -> None:
        try:
            self._shared_memory = shared_memory.SharedMemory(name=self.name, track=False)
        except TypeError:
            self._shared_memory = shared_memory.SharedMemory(name=self.name)
            _untrack_shared_memory(self._shared_memory)


def _shareable_list_data_offset(shm: shared_memory.SharedMemory, index: int) -> int:
    list_len = struct.unpack_from("q", shm.buf, 0)[0]
    if index < 0 or index >= list_len:
        raise IndexError("Shared state memory index out of range")
    return (list_len + 2 + index) * 8


def _read_shareable_list_float(shm: shared_memory.SharedMemory, index: int) -> float:
    return float(struct.unpack_from("d", shm.buf, _shareable_list_data_offset(shm, index))[0])


def _read_shareable_list_int(shm: shared_memory.SharedMemory, index: int) -> int:
    return int(struct.unpack_from("q", shm.buf, _shareable_list_data_offset(shm, index))[0])


def _untrack_shared_memory(shm: shared_memory.SharedMemory) -> None:
    try:
        from multiprocessing import resource_tracker

        resource_tracker.unregister(shm._name, "shared_memory")
    except Exception:
        logger.debug("Failed to unregister shared memory %r", shm.name, exc_info=True)
