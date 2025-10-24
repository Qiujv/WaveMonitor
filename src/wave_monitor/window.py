"""A simple GUI for monitoring waveforms."""

import logging
import sys
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from multiprocessing import shared_memory
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QMouseEvent, QPixmap, QShortcut
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .constants import CHUNK_SIZE, HEAD_LENGTH, PIPE_NAME, SHARED_MEMORY_NAME
from .proto import decode

try:
    __version__ = version("WaveMonitor")
except PackageNotFoundError:
    __version__ = "unknown"

about_message = (
    f"<b>Wave Monitor</b> v{__version__}<br><br>"
    "A simple GUI for monitoring waveforms.<br><br>"
    "by Jiawei Qiu"
)
N_VISIBLE_WFMS = 100
logger = logging.getLogger(__name__)


class DataSource(QLocalServer):
    """Receive messages from client and emit signals to trigger operation on monitor."""

    add_wfm = Signal(str, np.ndarray, list)
    remove_wfm = Signal(str)
    clear = Signal()
    autoscale = Signal()
    add_note = Signal(str, str)
    logger = logger.getChild("DataSource")

    def __init__(self, parent):
        super().__init__(parent=parent)
        self.frame_buffer: bytes = b""
        self.frame_length: int | None = None

        self.newConnection.connect(self.handle_new_connection)
        QApplication.instance().aboutToQuit.connect(self.close)

        # Remove previous instance. see https://doc.qt.io/qtforpython-6/PySide6/QtNetwork/QLocalServer.html#PySide6.QtNetwork.PySide6.QtNetwork.QLocalServer.removeServer
        self.removeServer(PIPE_NAME)  # Remove previous instance.
        self.listen(PIPE_NAME)

        self.logger.info('Listening on "%s".', self.fullServerName())

    def handle_new_connection(self):
        self.close_client_connection()  # Close previous connection.
        self.client_connection = self.nextPendingConnection()
        self.client_connection.readyRead.connect(self.read_frame)
        self.client_connection.disconnected.connect(
            lambda: self.logger.info("Client disconnected.")
        )
        self.logger.info("New client connected.")

    def read_frame(self):
        while self.client_connection.bytesAvailable():
            self.logger.debug(">>> Buffer updated")
            if self.frame_length is None:
                if self.client_connection.bytesAvailable() < HEAD_LENGTH:
                    self.logger.debug("Not enough data to read frame length.")
                    return  # Wait for next buffer update.
                data = self.client_connection.read(HEAD_LENGTH).data()
                try:
                    self.frame_length = int.from_bytes(data, "big")
                    self.logger.info("Expecting {:,} bytes.".format(self.frame_length))
                except Exception:
                    self.frame_length = None
                    self.logger.exception("Failed to parse frame length: %r", data)
                    continue  # Try again.

            expected = min(self.frame_length - len(self.frame_buffer), CHUNK_SIZE)
            available = self.client_connection.bytesAvailable()
            if available < expected:
                self.logger.debug(f"{expected=:,}, {available=:,}")
                return  # Wait for next buffer update.

            data = self.client_connection.read(expected).data()
            self.logger.debug(f"Received {len(data):,} bytes.")
            self.frame_buffer += data

            if len(self.frame_buffer) < self.frame_length:
                continue  # Proceed to read the rest.

            try:
                msg = decode(self.frame_buffer)
            except Exception:
                msg = None
                self.logger.exception("Failed to parse msg: %r", self.frame_buffer)

            self.frame_buffer = b""
            self.frame_length = None
            self.logger.info("<<< Received: %r", msg)
            self.handle_client_message(msg)

            continue  # There might be more data.

    def handle_client_message(self, msg: dict):
        if not isinstance(msg, dict):
            self.logger.warning("Invalid message format: %r", msg)
            return
        if "_type" not in msg:
            self.logger.warning("Message missing _type field: %r", msg)
            return

        if msg["_type"] == "add_wfm":
            self.add_wfm.emit(msg["name"], msg["t"], msg["ys"])
        elif msg["_type"] == "remove_wfm":
            self.remove_wfm.emit(msg["name"])
        elif msg["_type"] == "clear":
            self.clear.emit()
        elif msg["_type"] == "autoscale":
            self.autoscale.emit()
        elif msg["_type"] == "add_note":
            self.add_note.emit(msg["name"], msg["note"])
        else:
            self.logger.exception(f"Unknown message type: {msg['_type']}")

    def close_client_connection(self):
        if hasattr(self, "client_connection"):
            self.client_connection.readyRead.disconnect(self.read_frame)
            self.client_connection.close()  # Not working, because client not in qt event loop.

    def close(self):
        self.close_client_connection()
        self.logger.info('Closing server "%s".', self.fullServerName())
        super().close()


class SharedServerState:
    """Server state in shared memory."""

    DEFAULT_WFM_INTERVAL = 0.2  # seconds

    def __init__(self):
        self._shared_memory = None
        self.create_shared_memory()
        QApplication.instance().aboutToQuit.connect(self.cleanup_shared_memory)

    def create_shared_memory(self):
        try:
            old_shm = shared_memory.ShareableList(name=SHARED_MEMORY_NAME)
            self._shared_memory = old_shm
            logger.info(f"Reused existing shared memory '{SHARED_MEMORY_NAME}'")
        except FileNotFoundError:
            pass  # No existing shared memory.

        try:
            self._shared_memory = shared_memory.ShareableList(
                [self.DEFAULT_WFM_INTERVAL], name=SHARED_MEMORY_NAME
            )
            logger.info(f"Created shared memory '{SHARED_MEMORY_NAME}'")
        except Exception:
            logger.exception("Failed to create shared memory")

    def cleanup_shared_memory(self):
        if self._shared_memory is not None:
            try:
                self._shared_memory.shm.close()
                self._shared_memory.shm.unlink()
                logger.info("Cleaned up shared memory")
            except Exception:
                logger.exception("Error cleaning up shared memory")

    @property
    def wfm_interval(self) -> float:
        if self._shared_memory is not None:
            try:
                return float(self._shared_memory[0])
            except Exception:
                logger.exception("Failed to read wfm_interval from shared memory")
        return self.DEFAULT_WFM_INTERVAL

    @wfm_interval.setter
    def wfm_interval(self, value: float) -> None:
        if self._shared_memory is not None:
            try:
                self._shared_memory[0] = value
            except Exception:
                logger.exception("Failed to write wfm_interval to shared memory")


class MonitorWindow(QObject):
    """Keep some widgets and plot waveforms with them.

    This class creates the main window, the dock UI for controlling
    waveform display, and a background DataSource thread to receive
    messages from clients.
    """

    logger = logger.getChild("MonitorWindow")

    def __init__(self, wfm_separation: float = 2):
        super().__init__()
        MonitorWindow.setup_app_style(QApplication.instance())

        # Basic state
        self.wfm_separation = wfm_separation
        self.wfms: dict[str, "Waveform"] = {}
        self.state = SharedServerState()

        # Build UI and start server thread
        self._create_main_window()
        self._create_log_dock()
        self._create_dock()
        self._start_server()

        # Finalize
        self.window.show()
        self.logger.info("Ready. Right-click to show menu.")

    def _create_main_window(self) -> None:
        """Create the main window and plotting widget."""
        window = QMainWindow()
        window.setWindowTitle("Wave Monitor")
        window.setWindowIcon(
            QIcon(QPixmap(str(files("wave_monitor") / "assets" / "icon.svg")))
        )

        # Shortcuts
        QShortcut("F", window).activated.connect(self.autoscale)
        QShortcut("C", window).activated.connect(self.confirm_clear)
        QShortcut("R", window).activated.connect(self.refresh_plots)
        QShortcut("Shift+A", window).activated.connect(self._add_test_wfm)
        QShortcut("Shift+1", window).activated.connect(self._add_test_wfm1)

        # Plot widget
        plot_widget = pg.plot(parent=window)
        window.setCentralWidget(plot_widget)
        plot_item = plot_widget.getPlotItem()
        plot_item.showGrid(x=True, y=True)
        plot_item.setDownsampling(auto=True, mode="subsample")
        plot_item.setClipToView(True)
        # ClipToView disables plot_item.autoRange, as well as "View all" in right-click menu.
        plot_item.getViewBox().disableAutoRange()
        plot_item.getViewBox().setMenuEnabled(False)  # Disable the menu by pyqtgraph.

        # Right-click filter
        _filter = RightClickFilter(self.show_context_menu)
        # viewport gets the mouseReleaseEvent, See https://blog.csdn.net/theoryll/article/details/110918779
        plot_widget.viewport().installEventFilter(_filter)
        self._right_click_filter = _filter

        # Store references
        self.window = window
        self.plot_widget = plot_widget
        self.plot_item = plot_item

    def _create_dock(self) -> None:
        """Create the side dock containing wfms list and controls."""
        dock_widget = QDockWidget(f"wfms⪅{N_VISIBLE_WFMS}", self.window)
        dock_widget.setFloating(False)
        self.window.addDockWidget(Qt.RightDockWidgetArea, dock_widget)

        font_metrics = dock_widget.fontMetrics()
        initial_width = font_metrics.horizontalAdvance("X") * 15
        self.window.resizeDocks([dock_widget], [initial_width], Qt.Horizontal)

        dock_layout = QVBoxLayout()

        # List widget for wfms
        list_widget = QListWidget()
        list_widget.setDragDropMode(QListWidget.InternalMove)
        list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        list_widget.customContextMenuRequested.connect(self.show_list_context_menu)
        _filter = DeleteEventFilter(self.remove_wfm, list_widget)
        list_widget.installEventFilter(_filter)
        self._delete_event_filter = _filter
        dock_layout.addWidget(list_widget)

        # Separation input
        input_layout = QHBoxLayout()
        label = QLabel("sep. ")
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        input_layout.addWidget(label)
        wfm_separation_input = QDoubleSpinBox()
        wfm_separation_input.setValue(self.wfm_separation)
        wfm_separation_input.setMinimum(0)
        wfm_separation_input.setSingleStep(0.5)
        wfm_separation_input.setDecimals(1)
        wfm_separation_input.valueChanged.connect(
            lambda value: setattr(self, "wfm_separation", value)
        )
        wfm_separation_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        input_layout.addWidget(wfm_separation_input)
        dock_layout.addLayout(input_layout)

        # wfm_interval input
        interval_layout = QHBoxLayout()
        interval_label = QLabel("wfm_interval (s): ")
        interval_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        interval_layout.addWidget(interval_label)
        wfm_interval_input = QDoubleSpinBox()
        wfm_interval_input.setValue(self.state.wfm_interval)
        wfm_interval_input.setMinimum(0)
        wfm_interval_input.setSingleStep(0.1)
        wfm_interval_input.setDecimals(1)
        wfm_interval_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        interval_layout.addWidget(wfm_interval_input)
        dock_layout.addLayout(interval_layout)
        wfm_interval_input.valueChanged.connect(
            lambda v: setattr(self.state, "wfm_interval", v)
        )

        dock_layout.setSpacing(1)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(1)
        input_layout.setContentsMargins(0, 0, 0, 0)

        dock_content = QWidget()
        dock_content.setLayout(dock_layout)
        dock_widget.setWidget(dock_content)

        self.dock_widget = dock_widget
        self.list_widget = list_widget

    def _create_log_dock(self) -> None:
        """Create a dock to show log messages."""
        log_dock = QDockWidget("Log", self.window)
        log_dock.setFloating(False)
        log_view = QPlainTextEdit()
        log_view.setReadOnly(True)
        log_view.setMaximumBlockCount(1000)
        log_content = QWidget()
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(log_view)
        log_content.setLayout(log_layout)
        log_dock.setWidget(log_content)
        self.window.addDockWidget(Qt.BottomDockWidgetArea, log_dock)
        log_dock.hide()
        self.log_view = log_view
        self.log_dock = log_dock

        try:
            self._log_signal = LogSignal()
            self._log_signal.log.connect(lambda s: self.log_view.appendPlainText(s))
            handler = QtHandler(self._log_signal)
            logger.addHandler(handler)
        except Exception:
            logger.exception("Failed to initialize GUI log handler")

    def _start_server(self) -> None:
        """Start the DataSource thread to receive client messages."""
        server = DataSource(self)
        server.add_wfm.connect(self.add_wfm)
        server.remove_wfm.connect(self.remove_wfm)
        server.clear.connect(self.client_clear)
        server.autoscale.connect(self.autoscale)
        server.add_note.connect(self.add_note)
        self.server = server

    def add_wfm(self, name: str, t: np.ndarray, ys: list[np.ndarray]):
        if name in self.wfms:
            wfm = self.wfms[name]
            wfm.update_wfm(t, ys)
        else:
            visible_wfms = self.visible_wfms
            offset = self.wfm_separation * len(visible_wfms)
            wfm = Waveform(name, t, ys, offset, self.plot_item, self.list_widget)
            if len(visible_wfms) >= N_VISIBLE_WFMS:
                wfm.set_visible(False)
            self.wfms[name] = wfm

    def remove_wfm(self, name: str):
        if name in self.wfms:
            self.wfms[name].remove()
            del self.wfms[name]
        else:
            self.logger.warning("Waveform %s not found, nothing removed.", name)

    def clear(self):
        for name in list(self.wfms.keys()):
            self.remove_wfm(name)

    def client_clear(self):
        for wfm in self.wfms.values():
            wfm.update_wfm(np.array([0]), [np.array([0])])

    def confirm_clear(self):
        """Ask user to confirm before clearing all wfms."""
        reply = QMessageBox.question(
            self.window,
            "Clear all waveforms?",
            "Are you sure to clear all waveforms?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.clear()

    def autoscale(self):
        visible_wfms = self.visible_wfms
        if visible_wfms:
            t0 = min(wfm.t0 for wfm in visible_wfms)
            t1 = max(wfm.t1 for wfm in visible_wfms)
            y0 = min(wfm.offset for wfm in visible_wfms) - self.wfm_separation / 2
            y1 = max(wfm.offset for wfm in visible_wfms) + self.wfm_separation / 2
            self.plot_item.setRange(xRange=(t0, t1), yRange=(y0, y1))

    def add_note(self, name: str, note: str):
        if name in self.wfms:
            self.wfms[name].note.setHtml(note)
        else:
            self.logger.warning("Waveform %s not found, note not added.", name)

    def refresh_plots(self):
        for i, wfm in enumerate(self.visible_wfms[::-1]):
            wfm.update_offset(self.wfm_separation * i)

    @property
    def visible_wfms(self) -> list["Waveform"]:
        """Return a list of visible wfms, sorted as in list_widget."""
        list_wfms = []
        for name in self.list_names:
            wfm = self.wfms[name]
            if wfm.is_visible():
                list_wfms.append(wfm)
        return list_wfms

    @property
    def list_names(self) -> list[str]:
        """Return list of item names in list_widget, should be names of wfms."""
        list_widget = self.list_widget
        return [list_widget.item(i).text() for i in range(list_widget.count())]

    def restore_dock(self):
        if not self.dock_widget.isVisible():
            self.dock_widget.show()

    def restore_log_dock(self):
        if not self.log_dock.isVisible():
            self.log_dock.show()

    def show_context_menu(self, pos: QPointF):
        menu = QMenu(self.plot_widget)

        zoom_fit_action = QAction("Zoom fit (F)", self.window)
        zoom_fit_action.triggered.connect(self.autoscale)
        menu.addAction(zoom_fit_action)

        refresh_action = QAction("Refresh plots (R)", self.window)
        refresh_action.triggered.connect(self.refresh_plots)
        menu.addAction(refresh_action)

        dock_restore_action = QAction('Restore "wfms" list', self.window)
        dock_restore_action.triggered.connect(self.restore_dock)
        menu.addAction(dock_restore_action)

        log_restore_action = QAction('Restore "log" dock', self.window)
        log_restore_action.triggered.connect(self.restore_log_dock)
        menu.addAction(log_restore_action)

        # # Not working. But anyway, it is slow.
        # export_action = QAction("PyQtGraph Export (csv slow!)", self.window)
        # export_action.triggered.connect(self.plot_widget.sceneObj.showExportDialog)

        menu.addSeparator()

        about_action = QAction("About", self.window)
        about_action.triggered.connect(self.show_about_dialog)
        menu.addAction(about_action)

        menu.exec(self.plot_widget.mapToGlobal(pos.toPoint()))

    def show_list_context_menu(self, pos: QPoint):
        menu = QMenu(self.list_widget)

        show_action = QAction("Show selected", self.dock_widget)

        def show_selected_wfms():
            for item in self.list_widget.selectedItems():
                self.wfms[item.text()].set_visible(True)

        show_action.triggered.connect(show_selected_wfms)
        menu.addAction(show_action)

        hide_action = QAction("Hide selected", self.dock_widget)

        def hide_selected_wfms():
            for item in self.list_widget.selectedItems():
                self.wfms[item.text()].set_visible(False)

        hide_action.triggered.connect(hide_selected_wfms)
        menu.addAction(hide_action)

        remove_action = QAction("Remove selected (Del)", self.dock_widget)

        def remove_selected_wfms():
            for item in self.list_widget.selectedItems():
                self.remove_wfm(item.text())

        remove_action.triggered.connect(remove_selected_wfms)
        menu.addAction(remove_action)

        clear_action = QAction("Clear all (C)", self.window)
        clear_action.triggered.connect(self.confirm_clear)
        menu.addAction(clear_action)

        sort_action = QAction("Sort list", self.window)
        sort_action.triggered.connect(self.list_widget.sortItems)
        menu.addAction(sort_action)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def show_about_dialog(self):
        QMessageBox.about(self.window, "About Wave Monitor", about_message)

    @staticmethod
    def setup_app_style(app: QApplication) -> None:
        with open(files("wave_monitor") / "assets" / "style.qss", "r") as f:
            _style = f.read()
            app.setStyleSheet(_style)

    def _add_test_wfm(self):
        i = len(self.wfms)
        t = np.linspace(0, 1, 1_000_001)
        i_wave = np.cos(2 * np.pi * i * t, dtype=np.float16)
        q_wave = np.sin(2 * np.pi * i * t, dtype=np.float16)
        self.add_wfm(f"test_wfm_{i}", t, [i_wave, q_wave])

    def _add_test_wfm1(self):
        t = np.linspace(0, 1, 10_001)
        f = np.random.randint(3, 100)
        i_wave = np.cos(2 * np.pi * f * t, dtype=np.float16)
        q_wave = np.sin(2 * np.pi * f * t, dtype=np.float16)
        z_wave = np.random.rand(t.size)
        self.add_wfm("test_wfm_random", t, [i_wave, q_wave, z_wave])


class Waveform:
    """Container for all assets of a waveform."""

    colors = (
        # # Simple RBG
        # (255, 0, 0, 50),
        # (0, 0, 255, 50),
        # (0, 255, 0, 50),
        # "dark_background" in https://matplotlib.org/stable/gallery/style_sheets/style_sheets_reference.html
        (214, 98, 86, 80),
        (98, 144, 176, 80),
        (217, 147, 69, 80),
        (146, 188, 75, 80),
        (155, 99, 156, 80),
        (170, 200, 163, 80),
        (219, 202, 81, 80),
        (110, 177, 166, 80),
        (218, 219, 146, 80),
        (158, 154, 183, 80),
    )

    def __init__(
        self,
        name: str,
        t: np.ndarray,
        ys: list[np.ndarray],
        offset: float,
        plot_item: pg.PlotItem,
        list_widget: QListWidget,
        note: str = "",
    ):
        """Add line plot to plot_item, add checkbox to list_widget."""
        text = pg.TextItem(text=name, anchor=(1, 0.5))
        note = pg.TextItem(text=note, anchor=(0, 0.5))
        plot_item.addItem(text)
        plot_item.addItem(note)
        plot_item.sigXRangeChanged.connect(self.update_label_pos)

        list_item = QListWidgetItem(name)
        list_item.setFlags(list_item.flags() | Qt.ItemIsUserCheckable)  # Add checkbox.
        list_item.setCheckState(Qt.Checked)
        # QListWidgetItem is not a QObject, so it can't emit signals.
        # The checkbox state change is emitted by QListWidget.
        list_widget.itemChanged.connect(self.handel_checkbox_change)
        list_widget.insertItem(0, list_item)
        self.plot_item = plot_item
        self.lines: list[pg.PlotDataItem] = []
        self.t = t
        self.ys = ys
        if self.t.size:
            self.t0 = self.t[0]
            self.t1 = self.t[-1]
        else:
            self.t0 = 0.0
            self.t1 = 0.0
        self.offset = offset
        self.text = text
        self.note = note
        self.update_label_pos()
        self.list_item = list_item
        self.list_widget = list_widget

        self.reset_plot_data()

    def update_wfm(self, t: np.ndarray, ys: list[np.ndarray]):
        self.t = t
        self.ys = ys
        if self.t.size:
            self.t0 = self.t[0]
            self.t1 = self.t[-1]
        else:
            self.t0 = 0.0
            self.t1 = 0.0
        self.reset_plot_data()

    def update_offset(self, offset: float):
        self.offset = offset
        self.reset_plot_data()

    def reset_plot_data(self):
        """Reset plot lines using stored waveform data."""
        expected = len(self.ys)
        current = len(self.lines)

        if current > expected:
            for line in self.lines[expected:]:
                self.plot_item.removeItem(line)
            self.lines = self.lines[:expected]
            current = expected

        if current < expected:
            for idx in range(current, expected):
                color = self.colors[idx % len(self.colors)]
                line = self.plot_item.plot(
                    self.t,
                    self.ys[idx] + self.offset,
                    pen=color[:-1],
                    fillLevel=self.offset,
                    fillBrush=color,
                )
                self.lines.append(line)

        for idx, line in enumerate(self.lines):
            color = self.colors[idx % len(self.colors)]
            line.setData(self.t, self.ys[idx] + self.offset)
            line.setPen(color[:-1])
            line.setFillLevel(self.offset)
            line.setFillBrush(color)

        self.update_label_pos()

    def remove(self):
        for line in self.lines:
            self.plot_item.removeItem(line)

        self.plot_item.removeItem(self.text)
        self.plot_item.removeItem(self.note)
        self.plot_item.sigXRangeChanged.disconnect(self.update_label_pos)

        row = self.list_widget.row(self.list_item)
        self.list_widget.takeItem(row)

    def update_label_pos(self):
        viewbox = self.plot_item.getViewBox()
        (x0, x1), (y0, y1) = viewbox.viewRange()
        if x1 <= self.t0:
            x = self.t0
        elif x1 <= self.t1:
            x = x1
        else:
            x = self.t1
        self.text.setPos(x, self.offset)
        self.note.setPos(x, self.offset)

    def set_visible(self, visible: bool):
        for line in self.lines:
            line.setVisible(visible)
        self.text.setVisible(visible)
        self.note.setVisible(visible)

        # Change checkbox state without triggering handel_checkbox_change.
        self.list_widget.itemChanged.disconnect(self.handel_checkbox_change)
        self.list_item.setCheckState(Qt.Checked if visible else Qt.Unchecked)
        self.list_widget.itemChanged.connect(self.handel_checkbox_change)

    def handel_checkbox_change(self, item: QListWidgetItem):
        """Triggered when the checkbox is clicked."""
        if item is self.list_item:
            self.set_visible(item.checkState() == Qt.Checked)

    def is_visible(self) -> bool:
        return self.text.isVisible()


class LogSignal(QObject):
    """A small QObject to carry log messages to the GUI thread."""

    log = Signal(str)


class QtHandler(logging.Handler):
    """A logging.Handler that emits records through a LogSignal."""

    def __init__(self, signal: LogSignal):
        super().__init__()
        self._signal = signal
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s", "%H:%M:%S"
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._signal.log.emit(msg)
        except Exception:
            self.handleError(record)


class RightClickFilter(QObject):
    def __init__(self, show_ctx_menu: Callable[[QPointF], None]):
        super().__init__()
        self.show_ctx_menu = show_ctx_menu
        self.mouse_press_pos = None

    def eventFilter(self, watched, event: QMouseEvent):
        # Filter the right-click instead dragging.
        if event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.RightButton:
                self.mouse_press_pos = event.position()
        if event.type() == QEvent.MouseButtonRelease:
            if event.button() == Qt.RightButton:
                if self.mouse_press_pos is not None:
                    if (event.position() - self.mouse_press_pos).manhattanLength() < 5:
                        self.show_ctx_menu(event.position())
        return super().eventFilter(watched, event)


class DeleteEventFilter(QObject):
    def __init__(self, remove_wfm: Callable[[str], None], list_widget: QListWidget):
        super().__init__()
        self.remove_wfm = remove_wfm
        self.list_widget = list_widget

    def eventFilter(self, source, event):
        if (
            source is self.list_widget
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Delete
        ):
            for item in self.list_widget.selectedItems():
                self.remove_wfm(item.text())
            return True
        return super().eventFilter(source, event)


class WindowCloseFilter(QObject):
    """Event filter to detect window close and stop the DataSource server."""

    def __init__(self, server: DataSource):
        super().__init__()
        self._server = server

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Close:
            try:
                # Stop the server thread cleanly when the window is closed.
                self._server.close()
            except Exception:
                logger.exception("Error while stopping DataSource on window close")
        return super().eventFilter(watched, event)


def config_log(default_loglevel: str = "INFO"):
    # Show all numpy array when printing.
    np.set_printoptions(precision=2, threshold=4, edgeitems=1, linewidth=500)

    # Get the log level from command line arguments, find pattern like "--log=DEBUG"
    loglevel = next(
        (arg.split("=")[1] for arg in sys.argv if arg.startswith("--log=")),
        default_loglevel,
    )
    numeric_level = getattr(logging, loglevel.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: %s" % loglevel)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

def start():
    """Console entry point.

    Accepts --log=<LEVEL> and passes it to config_log. Uses parse_known_args so
    Qt/other args are preserved for QApplication.
    """
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--log", help="Set log level (DEBUG, INFO, WARNING, ERROR)", default=None
    )
    args, _ = parser.parse_known_args()

    if args.log:
        config_log(args.log)
    else:
        config_log()

    app = QApplication(sys.argv)
    _ = MonitorWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    config_log("DEBUG")
    app = QApplication(sys.argv)
    _ = MonitorWindow()
    sys.exit(app.exec())
