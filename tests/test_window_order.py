import numpy as np

import wave_monitor.window as window_module
from wave_monitor.window import (
    MonitorWindow,
    WFM_ORDER_CACHE_LIMIT,
    WFM_ORDER_SETTINGS_KEY,
)


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


def add_demo_wfm(monitor, name):
    t = np.array([0.0, 1.0])
    y = np.array([0.0, 1.0])
    monitor.add_wfm(name, t, [y])


def test_cached_waveform_order_is_restored_as_waveforms_arrive(qapp, monkeypatch):
    settings = FakeSettings({WFM_ORDER_SETTINGS_KEY: ["a", "b", "c"]})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        add_demo_wfm(monitor, "b")
        add_demo_wfm(monitor, "a")
        add_demo_wfm(monitor, "c")

        assert monitor.list_names == ["a", "b", "c"]
    finally:
        monitor.window.close()


def test_unknown_waveform_keeps_current_insert_at_top_behavior(qapp, monkeypatch):
    settings = FakeSettings({WFM_ORDER_SETTINGS_KEY: ["a", "b"]})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        add_demo_wfm(monitor, "a")
        add_demo_wfm(monitor, "b")
        add_demo_wfm(monitor, "new")

        assert monitor.list_names == ["new", "a", "b"]
    finally:
        monitor.window.close()


def test_sort_list_persists_current_order(qapp, monkeypatch):
    settings = FakeSettings({WFM_ORDER_SETTINGS_KEY: ["missing"]})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        add_demo_wfm(monitor, "b")
        add_demo_wfm(monitor, "a")

        monitor.wfm_list_dock.sort_items()

        assert monitor.list_names == ["a", "b"]
        assert settings.values[WFM_ORDER_SETTINGS_KEY] == ["a", "b", "missing"]
        assert settings.synced
    finally:
        monitor.window.close()


def test_waveform_order_cache_is_capped(qapp, monkeypatch):
    cached_names = [f"old_{i}" for i in range(WFM_ORDER_CACHE_LIMIT + 5)]
    settings = FakeSettings({WFM_ORDER_SETTINGS_KEY: cached_names})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        assert len(monitor._preferred_wfm_order) == WFM_ORDER_CACHE_LIMIT
        assert len(settings.values[WFM_ORDER_SETTINGS_KEY]) == WFM_ORDER_CACHE_LIMIT
        assert settings.values[WFM_ORDER_SETTINGS_KEY][0] == "old_0"
        assert settings.synced
    finally:
        monitor.window.close()


def test_clear_saved_waveform_order(qapp, monkeypatch):
    settings = FakeSettings({WFM_ORDER_SETTINGS_KEY: ["a", "b"]})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        monitor.clear_saved_wfm_order()

        assert monitor._preferred_wfm_order == []
        assert settings.values[WFM_ORDER_SETTINGS_KEY] == []
        assert settings.synced
    finally:
        monitor.window.close()


def test_wfm_list_dock_can_clear_saved_waveform_order(qapp, monkeypatch):
    settings = FakeSettings({WFM_ORDER_SETTINGS_KEY: ["a", "b"]})
    monitor = create_monitor_with_settings(monkeypatch, settings)

    try:
        monitor.wfm_list_dock.clear_saved_order_requested.emit()

        assert monitor._preferred_wfm_order == []
        assert settings.values[WFM_ORDER_SETTINGS_KEY] == []
        assert settings.synced
    finally:
        monitor.window.close()
