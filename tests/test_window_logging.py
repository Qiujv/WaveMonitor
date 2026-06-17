import logging
import warnings

import numpy as np
import wave_monitor.window as window_module
from wave_monitor.window import LOG_DOCK_LEVEL_SETTINGS_KEY, MonitorWindow


class FakeSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.synced = False

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.synced = True


def create_monitor_with_settings(monkeypatch, settings):
    monkeypatch.setattr(window_module, "QSettings", lambda *args: settings)
    return MonitorWindow()


def test_log_dock_level_selector_updates_handler_level(qapp, monkeypatch):
    monitor = create_monitor_with_settings(monkeypatch, FakeSettings())
    try:
        assert monitor.log_dock.handler is not None
        assert monitor.log_dock.handler.level == logging.INFO

        monitor.log_dock.set_level("DEBUG")
        assert monitor.log_dock.handler.level == logging.DEBUG

        monitor.log_dock.set_level("ERROR")
        assert monitor.log_dock.handler.level == logging.ERROR
    finally:
        monitor.window.close()


def test_log_dock_level_is_restored_and_saved(qapp, monkeypatch):
    settings = FakeSettings({LOG_DOCK_LEVEL_SETTINGS_KEY: "ERROR"})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        assert monitor.log_dock.handler is not None
        assert monitor.log_dock.level == "ERROR"
        assert monitor.log_dock.handler.level == logging.ERROR

        monitor.log_dock.set_level("DEBUG")
    finally:
        monitor.window.close()

    assert settings.values[LOG_DOCK_LEVEL_SETTINGS_KEY] == "DEBUG"
    assert settings.synced


def test_log_dock_receives_window_logger_output(qapp, monkeypatch):
    monitor = create_monitor_with_settings(
        monkeypatch, FakeSettings({LOG_DOCK_LEVEL_SETTINGS_KEY: "DEBUG"})
    )
    try:
        assert window_module.logger.name == "wave_monitor.window"

        window_module.logger.getChild("DataSource").debug("probe log dock output")
        qapp.processEvents()

        assert "DEBUG wave_monitor.window.DataSource probe log dock output" in (
            monitor.log_view.toPlainText()
        )
    finally:
        monitor.window.close()


def test_log_dock_does_not_change_numpy_print_options(qapp, monkeypatch):
    before = np.get_printoptions()
    monitor = create_monitor_with_settings(
        monkeypatch, FakeSettings({LOG_DOCK_LEVEL_SETTINGS_KEY: "DEBUG"})
    )
    try:
        window_module.logger.debug("array probe %r", np.arange(100))
        qapp.processEvents()

        assert np.get_printoptions() == before
    finally:
        monitor.window.close()


def test_start_returns_when_server_already_exists(monkeypatch):
    created = False

    def fake_monitor_window():
        nonlocal created
        created = True

    monkeypatch.setattr(window_module, "can_connect_to_server", lambda name: True)
    monkeypatch.setattr(window_module, "MonitorWindow", fake_monitor_window)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        window_module.start()

    assert not created
    assert any("already listening" in str(w.message) for w in caught)
