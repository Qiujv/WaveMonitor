"""A simple GUI for monitoring waveforms."""

import logging
import sys
from importlib.resources import files
from typing import Callable

import msgpack
import msgpack_numpy
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QMouseEvent, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from wave_monitor.__about__ import __version__

PIPE_NAME = "wave_monitor"
about_message = (
    f"<b>Wave Monitor</b> v{__version__}<br><br>"
    "A simple GUI for monitoring waveforms.<br><br>"
    "by Jiawei Qiu"
)
logger = logging.getLogger(__name__)
HEAD_LENGTH = 10  # bytes
N_VISIBLE_WFMS = 100


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
        self.partial_msg: bytes = b""
        self.expected_msg_length: int = None

        self.newConnection.connect(self.handle_new_connection)
        QApplication.instance().aboutToQuit.connect(self.close)

        # Remove previous instance. see https://doc.qt.io/qtforpython-6/PySide6/QtNetwork/QLocalServer.html#PySide6.QtNetwork.PySide6.QtNetwork.QLocalServer.removeServer
        # self.removeServer(PIPE_NAME)  # Remove previous instance.
        self.listen(PIPE_NAME)

        self.logger.info('Listening on "%s".', PIPE_NAME)

    def handle_new_connection(self):
        self.close_client_connection()  # Close previous connection.
        self.client_connection = self.nextPendingConnection()
        self.client_connection.readyRead.connect(self.assmeble_message)
        self.client_connection.disconnected.connect(
            lambda: self.logger.info("Client disconnected.")
        )
        self.logger.info("New client connected.")

    def assmeble_message(self):
        # One readyRead signal may contain multiple messages.
        while self.client_connection.canReadLine():
            # Read the msg length.
            if self.expected_msg_length is None:
                if self.client_connection.bytesAvailable() < HEAD_LENGTH:
                    continue
                line = self.client_connection.read(HEAD_LENGTH).data()
                try:
                    self.expected_msg_length = int.from_bytes(line[:-1], "big")
                    logger.debug(f"Expecting {self.expected_msg_length} bytes for msg.")
                except:
                    logger.exception(f"Failed to parse msg length: {line}")
                    continue

            # Read the msg.
            if self.client_connection.bytesAvailable() < self.expected_msg_length:
                continue

            msg = self.client_connection.read(self.expected_msg_length).data()
            logger.debug(f"Received {len(msg)} bytes.")
            self.partial_msg += msg
            self.expected_msg_length -= len(msg)

            if len(self.partial_msg) < self.expected_msg_length:
                self.logger.debug(
                    "msg len: %s, expected: %s",
                    len(self.partial_msg),
                    self.expected_msg_length,
                )
                continue

            # Process the message
            msg = msgpack.unpackb(
                self.partial_msg[:-1], object_hook=msgpack_numpy.decode
            )
            self.partial_msg = b""
            self.expected_msg_length = None

            self.logger.debug(f"Received: {msg}")
            self.emit_signals(msg)

    def emit_signals(self, msg: dict):
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
        elif msg["_type"] == "are_you_there":
            self.client_connection.write(b"yes")
        else:
            raise ValueError(f"Unknown message type: {msg['_type']}")

    def close_client_connection(self):
        if hasattr(self, "client_connection"):
            self.client_connection.readyRead.disconnect(self.assmeble_message)
            self.client_connection.close()  # Not working, because client not in qt event loop.

    def close(self):
        self.close_client_connection()
        self.logger.info('Closing server "%s".', PIPE_NAME)
        super().close()


class MonitorWindow:
    """Keep some widgets and plot waveforms with them."""

    logger = logger.getChild("MonitorWindow")

    def __init__(self, wfm_separation: float = 2):
        MonitorWindow.setup_app_style(QApplication.instance())
        window = QMainWindow()
        window.setWindowTitle("Wave Monitor")
        window.setWindowIcon(QIcon(str(files("wave_monitor")/"assets"/"icon.png")))
        QShortcut("F", window).activated.connect(self.autoscale)
        QShortcut("C", window).activated.connect(self.confirm_clear)
        QShortcut("R", window).activated.connect(self.refresh_plots)
        QShortcut("Shift+A", window).activated.connect(self._add_test_wfm)
        QShortcut("Shift+1", window).activated.connect(self._add_test_wfm1)

        plot_widget = pg.plot(parent=window)
        window.setCentralWidget(plot_widget)

        plot_item = plot_widget.getPlotItem()
        plot_item.showGrid(x=True, y=True)
        # Make it hold millions of points.
        plot_item.setDownsampling(auto=True, mode="subsample")
        plot_item.setClipToView(True)
        # ClipToView disables plot_item.autoRange, as well as "View all" in right-click menu.
        plot_item.getViewBox().disableAutoRange()

        # Custom context menu.
        plot_item.getViewBox().setMenuEnabled(False)  # Disable the menu by pyqtgraph.
        _filter = RightClickFilter(self.show_context_menu)
        # viewport gets the mouseReleaseEvent, See https://blog.csdn.net/theoryll/article/details/110918779
        plot_widget.viewport().installEventFilter(_filter)
        self._right_click_filter = _filter

        dock_widget = QDockWidget(f"wfms⪅{N_VISIBLE_WFMS}", window)
        dock_widget.setFloating(False)
        window.addDockWidget(Qt.RightDockWidgetArea, dock_widget)
        font_metrics = dock_widget.fontMetrics()
        initial_width = font_metrics.horizontalAdvance("X") * 15  # 15 chars wide.
        window.resizeDocks([dock_widget], [initial_width], Qt.Horizontal)

        dock_layout = QVBoxLayout()
        list_widget = QListWidget()
        list_widget.setDragDropMode(QListWidget.InternalMove)
        list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        list_widget.customContextMenuRequested.connect(self.show_list_context_menu)
        _filter = DeleteEventFilter(self.remove_wfm, list_widget)
        list_widget.installEventFilter(_filter)
        self._delete_event_filter = _filter
        dock_layout.addWidget(list_widget)

        input_layout = QHBoxLayout()
        label = QLabel("sep. ")
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        input_layout.addWidget(label)
        wfm_separation_input = QDoubleSpinBox()
        wfm_separation_input.setValue(wfm_separation)
        wfm_separation_input.setMinimum(0)
        wfm_separation_input.setSingleStep(0.5)
        wfm_separation_input.setDecimals(1)
        wfm_separation_input.valueChanged.connect(
            lambda value: setattr(self, "wfm_separation", value)
        )
        wfm_separation_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        input_layout.addWidget(wfm_separation_input)
        dock_layout.addLayout(input_layout)

        dock_layout.setSpacing(1)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(1)
        input_layout.setContentsMargins(0, 0, 0, 0)
        dock_content = QWidget()
        dock_content.setLayout(dock_layout)
        dock_widget.setWidget(dock_content)

        server = DataSource(window)
        server.add_wfm.connect(self.add_wfm)
        server.remove_wfm.connect(self.remove_wfm)
        server.clear.connect(self.clear)
        server.autoscale.connect(self.autoscale)
        server.add_note.connect(self.add_note)

        window.show()
        self.logger.info("Ready. Right-click to show menu.")
        self.wfms: dict[str, "Waveform"] = {}
        self.window = window
        self.plot_widget = plot_widget
        self.plot_item = plot_item
        self.dock_widget = dock_widget
        self.list_widget = list_widget
        self.server = server
        self.wfm_separation = wfm_separation

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
            self.logger.warning(f"Waveform {name} not found, nothing removed.")

    def clear(self):
        for name in list(self.wfms.keys()):
            self.remove_wfm(name)

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
            self.logger.warning(f"Waveform {name} not found, note not added.")

    def refresh_plots(self):
        for i, wfm in enumerate(self.visible_wfms):
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
        with open(files("wave_monitor")/"assets"/"style.qss", "r") as f:
            _style = f.read()
            app.setStyleSheet(_style)

    def _add_test_wfm(self):
        i = len(self.wfms)
        t = np.linspace(0, 1, 1_000_001)
        i_wave = np.cos(2 * np.pi * i * t)
        q_wave = np.sin(2 * np.pi * i * t)
        self.add_wfm(f"test_wfm_{i}", t, [i_wave, q_wave])

    def _add_test_wfm1(self):
        t = np.linspace(0, 1, 10_001)
        f = np.random.randint(3, 100)
        i_wave = np.cos(2 * np.pi * f * t)
        q_wave = np.sin(2 * np.pi * f * t)
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
        lines: list[pg.PlotDataItem] = [
            plot_item.plot(
                t, y + offset, pen=color[:-1], fillLevel=offset, fillBrush=color
            )
            for y, color in zip(ys, self.colors)
        ]

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
        list_widget.addItem(list_item)

        self.offset = offset
        self.t0 = t[0]
        self.t1 = t[-1]
        self.plot_item = plot_item
        self.lines = lines
        self.text = text
        self.note = note
        self.update_label_pos()
        self.list_item = list_item
        self.list_widget = list_widget

    def update_wfm(self, t: np.ndarray, ys: list[np.ndarray]):
        # Update existing lines with new data.
        old_lines = self.lines
        new_lines = []
        for line, y in zip(self.lines, ys):
            line.setData(t, y + self.offset)
            new_lines.append(line)

        # Remove unused lines.
        if len(ys) < len(old_lines):
            for line in old_lines[len(ys) :]:
                self.plot_item.removeItem(line)

        # Add more lines if needed.
        if len(ys) > len(old_lines):
            for y, color in zip(ys[len(old_lines) :], self.colors[len(old_lines) :]):
                line = self.plot_item.plot(
                    t,
                    y + self.offset,
                    pen=color[:-1],
                    fillLevel=self.offset,
                    fillBrush=color,
                )
                new_lines.append(line)

        self.t0 = t[0]
        self.t1 = t[-1]
        self.lines = new_lines

    def update_offset(self, offset: float):
        old_offset = self.offset
        new_offset = offset
        for line in self.lines:
            t, y = line.getData()
            line.setData(t, y - old_offset + new_offset)
            line.setFillLevel(new_offset)
        self.offset = new_offset
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
            pos = self.t0
        elif x1 <= self.t1:
            pos = x1
        else:
            pos = self.t1
        self.text.setPos(pos, self.offset)
        self.note.setPos(pos, self.offset)

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


def config_log(dafault_loglevel="INFO"):
    # Get the log level from command line arguments, find pattern like "=-log=DEBUG"
    loglevel = next(
        (arg.split("=")[1] for arg in sys.argv if arg.startswith("--log=")),
        dafault_loglevel,
    )
    numeric_level = getattr(logging, loglevel.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: %s" % loglevel)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
