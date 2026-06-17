"""Dock widgets used by the monitor window."""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QDockWidget,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

N_VISIBLE_WFMS = 100
WFM_ORDER_SETTINGS_KEY = "waveform_order"
WFM_ORDER_CACHE_LIMIT = 10_000
LOG_DOCK_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_DOCK_LEVEL = "INFO"


class WaveformListDock(QDockWidget):
    """Dock widget that owns waveform ordering and list controls."""

    order_changed = Signal()
    show_selected = Signal(list)
    hide_selected = Signal(list)
    remove_selected = Signal(list)
    clear_requested = Signal()
    clear_saved_order_requested = Signal()
    separation_changed = Signal(float)
    interval_changed = Signal(float)

    def __init__(self, wfm_separation: float, wfm_interval: float, parent: QWidget):
        super().__init__(f"wfms⪅{N_VISIBLE_WFMS}", parent)
        self.setFloating(False)

        dock_layout = QVBoxLayout()
        list_widget = QListWidget()
        list_widget.setDragDropMode(QListWidget.InternalMove)
        list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        list_widget.customContextMenuRequested.connect(self._show_context_menu)
        list_widget.model().rowsMoved.connect(lambda *args: self.order_changed.emit())
        delete_event_filter = DeleteEventFilter(self._remove_selected, list_widget)
        list_widget.installEventFilter(delete_event_filter)
        dock_layout.addWidget(list_widget)

        sep_layout = QHBoxLayout()
        sep_label = QLabel("sep. ")
        sep_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        sep_layout.addWidget(sep_label)
        sep_input = QDoubleSpinBox()
        sep_input.setValue(wfm_separation)
        sep_input.setMinimum(0)
        sep_input.setSingleStep(0.5)
        sep_input.setDecimals(1)
        sep_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sep_input.valueChanged.connect(self.separation_changed.emit)
        sep_layout.addWidget(sep_input)
        dock_layout.addLayout(sep_layout)

        interval_layout = QHBoxLayout()
        interval_label = QLabel("wfm_interval (s): ")
        interval_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        interval_layout.addWidget(interval_label)
        interval_input = QDoubleSpinBox()
        interval_input.setValue(wfm_interval)
        interval_input.setMinimum(0)
        interval_input.setSingleStep(0.1)
        interval_input.setDecimals(1)
        interval_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        interval_input.valueChanged.connect(self.interval_changed.emit)
        interval_layout.addWidget(interval_input)
        dock_layout.addLayout(interval_layout)

        dock_layout.setSpacing(1)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        sep_layout.setSpacing(1)
        sep_layout.setContentsMargins(0, 0, 0, 0)

        dock_content = QWidget()
        dock_content.setLayout(dock_layout)
        self.setWidget(dock_content)

        self.list_widget = list_widget
        self._delete_event_filter = delete_event_filter

    @property
    def list_names(self) -> list[str]:
        return [
            self.list_widget.item(i).text() for i in range(self.list_widget.count())
        ]

    @property
    def selected_names(self) -> list[str]:
        return [item.text() for item in self.list_widget.selectedItems()]

    def sort_items(self) -> None:
        self.list_widget.sortItems()
        self.order_changed.emit()

    def _show_context_menu(self, pos: QPoint):
        menu = QMenu(self.list_widget)

        show_action = QAction("Show selected", self)
        show_action.triggered.connect(
            lambda: self.show_selected.emit(self.selected_names)
        )
        menu.addAction(show_action)

        hide_action = QAction("Hide selected", self)
        hide_action.triggered.connect(
            lambda: self.hide_selected.emit(self.selected_names)
        )
        menu.addAction(hide_action)

        remove_action = QAction("Remove selected (Del)", self)
        remove_action.triggered.connect(lambda checked=False: self._remove_selected())
        menu.addAction(remove_action)

        clear_action = QAction("Clear all (C)", self)
        clear_action.triggered.connect(
            lambda checked=False: self.clear_requested.emit()
        )
        menu.addAction(clear_action)

        sort_action = QAction("Sort list", self)
        sort_action.triggered.connect(lambda checked=False: self.sort_items())
        menu.addAction(sort_action)

        clear_saved_order_action = QAction("Clear saved order", self)
        clear_saved_order_action.triggered.connect(
            lambda checked=False: self.clear_saved_order_requested.emit()
        )
        menu.addAction(clear_saved_order_action)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def _remove_selected(self) -> None:
        self.remove_selected.emit(self.selected_names)


class LogDock(QDockWidget):
    """Dock widget that displays package log messages."""

    def __init__(
        self,
        package_logger: logging.Logger,
        parent: QWidget,
        level: str = DEFAULT_LOG_DOCK_LEVEL,
    ):
        super().__init__("Log", parent)
        self.setFloating(False)
        self._package_logger = package_logger
        self._level = _normalize_log_level(level)
        self._handler: LogDockHandler | None = None

        log_view = QPlainTextEdit()
        log_view.setReadOnly(True)
        log_view.setMaximumBlockCount(1000)
        log_content = QWidget()
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(log_view)
        log_content.setLayout(log_layout)
        self.setWidget(log_content)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.hide()

        self.log_view = log_view
        self._signal = LogSignal()
        self._signal.log.connect(self.log_view.appendPlainText)
        handler = LogDockHandler(self._signal)
        handler.setLevel(self._level)
        package_logger.addHandler(handler)
        package_logger.disabled = False
        package_logger.setLevel(logging.DEBUG)
        self._handler = handler

    @property
    def handler(self) -> LogDockHandler | None:
        return self._handler

    @property
    def level(self) -> str:
        return self._level

    def set_level(self, level_name: str) -> None:
        self._level = _normalize_log_level(level_name)
        self._package_logger.disabled = False
        self._package_logger.setLevel(logging.DEBUG)
        if self._handler is not None:
            self._handler.setLevel(self._level)

    def detach_handler(self) -> None:
        if self._handler is not None:
            self._package_logger.removeHandler(self._handler)
            self._handler = None

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        level_group = QActionGroup(menu)
        level_group.setExclusive(True)
        for level_name in LOG_DOCK_LEVELS:
            action = QAction(level_name, menu)
            action.setCheckable(True)
            action.setChecked(level_name == self._level)
            action.triggered.connect(
                lambda checked=False, level_name=level_name: self.set_level(level_name)
            )
            level_group.addAction(action)
            menu.addAction(action)
        menu.exec(self.mapToGlobal(pos))


class LogSignal(QObject):
    """A small QObject to carry log messages to the GUI thread."""

    log = Signal(str)


def _normalize_log_level(level_name: str) -> str:
    level = str(level_name).upper()
    if level not in LOG_DOCK_LEVELS:
        return DEFAULT_LOG_DOCK_LEVEL
    return level


class LogDockHandler(logging.Handler):
    """A logging.Handler that sends records to MonitorWindow's log dock."""

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
            with np.printoptions(precision=2, threshold=4, edgeitems=1, linewidth=500):
                msg = self.format(record)
            self._signal.log.emit(msg)
        except Exception:
            self.handleError(record)


class DeleteEventFilter(QObject):
    def __init__(self, remove_selected: Callable[[], None], list_widget: QListWidget):
        super().__init__(list_widget)
        self.remove_selected = remove_selected
        self.list_widget = list_widget

    def eventFilter(self, source, event):
        if (
            source is self.list_widget
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Delete
        ):
            self.remove_selected()
            return True
        return super().eventFilter(source, event)
