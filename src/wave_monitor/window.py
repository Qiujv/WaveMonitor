"""A simple GUI for monitoring waveforms."""

import logging
import sys
import warnings
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPointF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QMouseEvent, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
)

from .constants import PIPE_NAME
from .docks import (
    DEFAULT_LOG_DOCK_LEVEL,
    N_VISIBLE_WFMS,
    WFM_ORDER_CACHE_LIMIT,
    WFM_ORDER_SETTINGS_KEY,
    LogDock,
    WaveformListDock,
)
from .ipc.launcher import can_connect_to_server
from .ipc.messages import MessageReader
from .ipc.state_memory import ServerStateMemory

try:
    __version__ = version("WaveMonitor")
except PackageNotFoundError:
    __version__ = "unknown"

about_message = (
    f"<b>Wave Monitor</b> v{__version__}<br><br>"
    "A simple GUI for monitoring waveforms.<br><br>"
    "by Jiawei Qiu"
)
logger = logging.getLogger("wave_monitor.window")
package_logger = logging.getLogger("wave_monitor")
LOG_DOCK_LEVEL_SETTINGS_KEY = "log_dock_level"


class DataSource(QLocalServer):
    """Receive messages from client and emit signals to trigger operation on monitor."""

    add_wfm = Signal(str, np.ndarray, list)
    remove_wfm = Signal(str)
    clear = Signal()
    autoscale = Signal()
    add_note = Signal(str, str)
    close_window = Signal()
    logger = logger.getChild("DataSource")

    def __init__(self, parent):
        super().__init__(parent=parent)
        self._client_readers: dict[QLocalSocket, MessageReader] = {}
        self._client_ids: dict[QLocalSocket, int] = {}
        self._client_pollers: dict[QLocalSocket, QTimer] = {}
        self._next_client_id = 1
        self._closed = False

        self.newConnection.connect(self.handle_new_connection)
        self._listen()

        self.logger.info('Listening on "%s".', self.fullServerName())

    def handle_new_connection(self):
        while self.hasPendingConnections():
            client_connection = self.nextPendingConnection()
            client_id = self._next_client_id
            self._next_client_id += 1
            self._client_readers[client_connection] = MessageReader()
            self._client_ids[client_connection] = client_id
            client_connection.readyRead.connect(
                lambda sock=client_connection: self.read_frame(sock)
            )
            client_connection.disconnected.connect(
                lambda sock=client_connection: self.close_client_connection(sock)
            )
            poller = QTimer(self)
            poller.setInterval(20)
            poller.timeout.connect(lambda sock=client_connection: self.read_frame(sock))
            self._client_pollers[client_connection] = poller
            poller.start()
            self.logger.info("Client %s connected.", client_id)

    def read_frame(self, client_connection: QLocalSocket):
        reader = self._client_readers.get(client_connection)
        client_id = self._client_ids.get(client_connection, "?")
        if reader is None:
            self.logger.warning(
                "Received data from unknown client connection %s.", client_id
            )
            return
        for msg in reader.read_available(client_connection):
            if msg is None:
                self.logger.warning("Failed to parse client %s message.", client_id)
                continue
            self.logger.debug("<<< Received from client %s: %r", client_id, msg)
            self.handle_client_message(msg)

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
        elif msg["_type"] == "close_window":
            self.close_window.emit()
        elif msg["_type"] == "_set_ipc_probe_value":
            self.set_ipc_probe_value(msg)
        else:
            self.logger.warning("Unknown message type: %s", msg["_type"])

    def set_ipc_probe_value(self, msg: dict):
        try:
            self.parent().state.ipc_probe_value = int(msg["value"])
        except Exception:
            self.logger.exception("Failed to set IPC probe value.")

    def _listen(self):
        if self.listen(PIPE_NAME):
            return
        if can_connect_to_server(PIPE_NAME):
            raise RuntimeError(
                f'Another WaveMonitor server is already listening on "{PIPE_NAME}".'
            )

        # Remove stale socket left by a crashed previous instance, then retry.
        self.removeServer(PIPE_NAME)
        if not self.listen(PIPE_NAME):
            raise RuntimeError(
                f'Failed to listen on "{PIPE_NAME}": {self.errorString()}'
            )

    def close_client_connection(self, client_connection: QLocalSocket):
        if client_connection not in self._client_readers:
            return
        try:
            client_connection.readyRead.disconnect()
        except Exception:
            pass
        try:
            client_connection.disconnected.disconnect()
        except Exception:
            pass
        client_id = self._client_ids.pop(client_connection, "?")
        self._client_readers.pop(client_connection, None)
        poller = self._client_pollers.pop(client_connection, None)
        if poller is not None:
            poller.stop()
            poller.deleteLater()
        client_connection.close()
        self.logger.info("Client %s disconnected.", client_id)

    def close_client_connections(self):
        for client_connection in list(self._client_readers):
            self.close_client_connection(client_connection)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.close_client_connections()
        self.logger.info('Closing server "%s".', self.fullServerName())
        super().close()
        self.removeServer(PIPE_NAME)


class MonitorWindow(QObject):
    """Keep some widgets and plot waveforms with them.

    This class creates the main window, the dock UI for controlling
    waveform display, and a local socket server to receive messages
    from clients.
    """

    logger = logger.getChild("MonitorWindow")

    def __init__(self, wfm_separation: float = 2):
        super().__init__()
        MonitorWindow.setup_app_style(QApplication.instance())

        # Basic state
        self.wfm_separation = wfm_separation
        self.wfms: dict[str, "Waveform"] = {}
        self.state = ServerStateMemory()
        self._settings = QSettings("WaveMonitor", "WaveMonitor")
        self._preferred_wfm_order = self._load_wfm_order()
        self._resources_closed = False
        self._package_logger_level = package_logger.level

        # Build UI and start server thread
        self._create_main_window()
        self._create_log_dock()
        self._create_dock()
        self._start_server()

        # Finalize
        QApplication.instance().aboutToQuit.connect(self._close_resources)
        self.window.show()
        self.logger.info("Ready. Right-click to show menu.")

    def _create_main_window(self) -> None:
        """Create the main window and plotting widget."""
        window = QMainWindow()
        window.setWindowTitle("Wave Monitor")
        icon = QIcon(str(files("wave_monitor") / "assets" / "icon.svg"))
        window.setWindowIcon(icon)
        QApplication.instance().setWindowIcon(icon)

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
        wfm_list_dock = WaveformListDock(
            self.wfm_separation,
            self.state.wfm_interval,
            self.window,
        )
        self.window.addDockWidget(Qt.RightDockWidgetArea, wfm_list_dock)

        font_metrics = wfm_list_dock.fontMetrics()
        initial_width = font_metrics.horizontalAdvance("X") * 15
        self.window.resizeDocks([wfm_list_dock], [initial_width], Qt.Horizontal)

        wfm_list_dock.order_changed.connect(self._handle_list_order_changed)
        wfm_list_dock.show_selected.connect(self.show_wfms)
        wfm_list_dock.hide_selected.connect(self.hide_wfms)
        wfm_list_dock.remove_selected.connect(self.remove_wfms)
        wfm_list_dock.clear_requested.connect(self.confirm_clear)
        wfm_list_dock.clear_saved_order_requested.connect(self.clear_saved_wfm_order)
        wfm_list_dock.separation_changed.connect(self._set_wfm_separation)
        wfm_list_dock.interval_changed.connect(
            lambda value: setattr(self.state, "wfm_interval", value)
        )

        self.wfm_list_dock = wfm_list_dock
        self.dock_widget = wfm_list_dock
        self.list_widget = wfm_list_dock.list_widget

    def _create_log_dock(self) -> None:
        """Create a dock to show log messages."""
        log_level = str(
            self._settings.value(LOG_DOCK_LEVEL_SETTINGS_KEY, DEFAULT_LOG_DOCK_LEVEL)
        )
        log_dock = LogDock(package_logger, self.window, level=log_level)
        self.window.addDockWidget(Qt.BottomDockWidgetArea, log_dock)
        self.log_dock = log_dock
        self.log_view = log_dock.log_view

    def _start_server(self) -> None:
        """Start the local socket server to receive client messages."""
        server = DataSource(self)
        server.add_wfm.connect(self.add_wfm)
        server.remove_wfm.connect(self.remove_wfm)
        server.clear.connect(self.client_clear)
        server.autoscale.connect(self.autoscale)
        server.add_note.connect(self.add_note)
        server.close_window.connect(lambda: QTimer.singleShot(0, self.window.close))
        self.server = server
        close_filter = WindowCloseFilter(self._close_resources)
        self.window.installEventFilter(close_filter)
        self._window_close_filter = close_filter

    def _close_resources(self):
        if self._resources_closed:
            return
        self._resources_closed = True
        if hasattr(self, "server"):
            self.server.close()
        self.state.close()
        self._settings.setValue(LOG_DOCK_LEVEL_SETTINGS_KEY, self.log_dock.level)
        self._settings.sync()
        self.log_dock.detach_handler()
        package_logger.setLevel(self._package_logger_level)

    def _set_wfm_separation(self, value: float) -> None:
        self.wfm_separation = value

    def add_wfm(self, name: str, t: np.ndarray, ys: list[np.ndarray]):
        if name in self.wfms:
            wfm = self.wfms[name]
            wfm.update_wfm(t, ys)
        else:
            visible_wfms = self.visible_wfms
            visible = len(visible_wfms) < N_VISIBLE_WFMS
            offset = self.wfm_separation * len(visible_wfms)
            insert_index = self._insert_index_for_wfm(name)
            wfm = Waveform(
                name,
                t,
                ys,
                offset,
                self.plot_item,
                self.list_widget,
                insert_index=insert_index,
                visible=visible,
            )
            self.wfms[name] = wfm
            if visible:
                self.refresh_plots()

    def remove_wfm(self, name: str):
        if name in self.wfms:
            self.wfms[name].remove()
            del self.wfms[name]
        else:
            self.logger.warning("Waveform %s not found, nothing removed.", name)

    def remove_wfms(self, names: list[str]) -> None:
        for name in names:
            self.remove_wfm(name)

    def show_wfms(self, names: list[str]) -> None:
        for name in names:
            self.wfms[name].set_visible(True)
        self.refresh_plots()

    def hide_wfms(self, names: list[str]) -> None:
        for name in names:
            self.wfms[name].set_visible(False)
        self.refresh_plots()

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
            self.wfms[name].set_note(note)
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
        return self.wfm_list_dock.list_names

    def _load_wfm_order(self) -> list[str]:
        value = self._settings.value(WFM_ORDER_SETTINGS_KEY, [])
        if isinstance(value, str):
            names = [value]
        elif value is None:
            names = []
        else:
            names = [str(name) for name in value]

        capped_names = names[:WFM_ORDER_CACHE_LIMIT]
        if len(capped_names) != len(names):
            self._settings.setValue(WFM_ORDER_SETTINGS_KEY, capped_names)
            self._settings.sync()
        return capped_names

    def _save_wfm_order(self) -> None:
        current_names = self.list_names
        current_set = set(current_names)
        missing_cached_names = [
            name for name in self._preferred_wfm_order if name not in current_set
        ]
        self._preferred_wfm_order = (current_names + missing_cached_names)[
            :WFM_ORDER_CACHE_LIMIT
        ]
        self._settings.setValue(WFM_ORDER_SETTINGS_KEY, self._preferred_wfm_order)
        self._settings.sync()

    def clear_saved_wfm_order(self) -> None:
        self._preferred_wfm_order = []
        self._settings.setValue(WFM_ORDER_SETTINGS_KEY, [])
        self._settings.sync()

    def _handle_list_order_changed(self) -> None:
        self._save_wfm_order()
        self.refresh_plots()

    def _insert_index_for_wfm(self, name: str) -> int:
        try:
            name_order = self._preferred_wfm_order.index(name)
        except ValueError:
            return 0

        order_index = {
            cached_name: i for i, cached_name in enumerate(self._preferred_wfm_order)
        }
        for row, existing_name in enumerate(self.list_names):
            existing_order = order_index.get(existing_name)
            if existing_order is not None and existing_order > name_order:
                return row
        return self.list_widget.count()

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

        refresh_action = QAction("Refresh (R)", self.window)
        refresh_action.triggered.connect(self.refresh_plots)
        menu.addAction(refresh_action)

        dock_restore_action = QAction('Show "wfms" dock', self.window)
        dock_restore_action.triggered.connect(self.restore_dock)
        menu.addAction(dock_restore_action)

        log_restore_action = QAction('Show "log" dock', self.window)
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

    def show_about_dialog(self):
        message = QMessageBox(self.window)
        message.setWindowTitle("About Wave Monitor")
        message.setText(about_message)
        message.setInformativeText(
            f"Cached waveform order names: {len(self._preferred_wfm_order):,} "
            f"/ {WFM_ORDER_CACHE_LIMIT:,}"
        )
        message.setStandardButtons(QMessageBox.Ok)
        message.exec()

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
        insert_index: int = 0,
        visible: bool = True,
    ):
        """Store waveform data and create plot items lazily when visible."""
        list_item = QListWidgetItem(name)
        list_item.setFlags(list_item.flags() | Qt.ItemIsUserCheckable)  # Add checkbox.
        list_item.setCheckState(Qt.Checked if visible else Qt.Unchecked)
        # QListWidgetItem is not a QObject, so it can't emit signals.
        # The checkbox state change is emitted by QListWidget.
        list_widget.itemChanged.connect(self.handel_checkbox_change)
        list_widget.insertItem(insert_index, list_item)
        self.name = name
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
        self.note_text = note
        self.text: pg.TextItem | None = None
        self.note: pg.TextItem | None = None
        self._plot_items_connected = False
        self.list_item = list_item
        self.list_widget = list_widget

        if visible:
            self.ensure_plot_items()
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
        if self.is_visible():
            self.reset_plot_data()

    def update_offset(self, offset: float):
        self.offset = offset
        if self.is_visible():
            self.update_line_offsets()
            self.update_label_pos()

    def ensure_plot_items(self):
        if self.text is not None and self.note is not None:
            return
        self.text = pg.TextItem(text=self.name, anchor=(1, 0.5))
        self.note = pg.TextItem(text=self.note_text, anchor=(0, 0.5))
        self.plot_item.addItem(self.text)
        self.plot_item.addItem(self.note)
        if not self._plot_items_connected:
            self.plot_item.sigXRangeChanged.connect(self.update_label_pos)
            self._plot_items_connected = True
        self.update_label_pos()

    def reset_plot_data(self):
        """Reset plot lines using stored waveform data."""
        self.ensure_plot_items()
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
                    self.ys[idx],
                    pen=color[:-1],
                    fillLevel=0,
                    fillBrush=color,
                )
                line.setPos(0, self.offset)
                self.lines.append(line)

        for idx, line in enumerate(self.lines):
            color = self.colors[idx % len(self.colors)]
            line.setData(self.t, self.ys[idx])
            line.setPos(0, self.offset)
            line.setPen(color[:-1])
            line.setFillLevel(0)
            line.setFillBrush(color)

        self.update_label_pos()

    def update_line_offsets(self):
        for line in self.lines:
            line.setPos(0, self.offset)

    def set_note(self, note: str):
        self.note_text = note
        if self.note is not None:
            self.note.setHtml(note)

    def remove(self):
        for line in self.lines:
            self.plot_item.removeItem(line)
        self.lines.clear()

        if self.text is not None:
            self.plot_item.removeItem(self.text)
            self.text = None
        if self.note is not None:
            self.plot_item.removeItem(self.note)
            self.note = None
        if self._plot_items_connected:
            try:
                self.plot_item.sigXRangeChanged.disconnect(self.update_label_pos)
            except Exception:
                pass
            self._plot_items_connected = False

        row = self.list_widget.row(self.list_item)
        self.list_widget.takeItem(row)

    def update_label_pos(self):
        if self.text is None or self.note is None:
            return
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
        if visible:
            self.ensure_plot_items()
            self.reset_plot_data()
        for line in self.lines:
            line.setVisible(visible)
        if self.text is not None:
            self.text.setVisible(visible)
        if self.note is not None:
            self.note.setVisible(visible)

        # Change checkbox state without triggering handel_checkbox_change.
        try:
            self.list_widget.itemChanged.disconnect(self.handel_checkbox_change)
        except Exception:
            pass
        try:
            self.list_item.setCheckState(Qt.Checked if visible else Qt.Unchecked)
        finally:
            self.list_widget.itemChanged.connect(self.handel_checkbox_change)

    def handel_checkbox_change(self, item: QListWidgetItem):
        """Triggered when the checkbox is clicked."""
        if item is self.list_item:
            self.set_visible(item.checkState() == Qt.Checked)

    def is_visible(self) -> bool:
        return self.list_item.checkState() == Qt.Checked


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


class WindowCloseFilter(QObject):
    """Event filter to detect window close and run cleanup."""

    def __init__(self, close_resources: Callable[[], None]):
        super().__init__()
        self._close_resources = close_resources

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Close:
            try:
                self._close_resources()
            except Exception:
                logger.exception("Error while closing resources on window close")
        return super().eventFilter(watched, event)


def start():
    """Console entry point."""
    if can_connect_to_server(PIPE_NAME):
        warnings.warn(
            f'Another WaveMonitor server is already listening on "{PIPE_NAME}".'
        )
        return

    app = QApplication(sys.argv)
    _ = MonitorWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    start()
