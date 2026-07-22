"""Unit tests for the pywebview JS-bridge, without requiring pywebview to be installed.

``Api.pick_folder`` only imports ``webview`` once it has a window to call into, so the "no window"
path needs nothing installed; the other paths inject a fake ``webview`` module into ``sys.modules``
so the real optional dependency is never required just to test this thin wrapper.
"""

import sys
import types

from housekeeper.desktop import Api


class _FakeWindow:
    def __init__(self, result):
        self._result = result

    def create_file_dialog(self, dialog_type):
        return self._result


def _install_fake_webview(monkeypatch):
    fake = types.ModuleType("webview")
    fake.FOLDER_DIALOG = "folder"
    monkeypatch.setitem(sys.modules, "webview", fake)


def test_pick_folder_returns_none_without_a_window():
    assert Api().pick_folder() is None


def test_pick_folder_returns_first_selected_path(monkeypatch):
    _install_fake_webview(monkeypatch)
    api = Api()
    api.window = _FakeWindow(("/chosen/path",))
    assert api.pick_folder() == "/chosen/path"


def test_pick_folder_returns_none_when_dialog_is_cancelled(monkeypatch):
    _install_fake_webview(monkeypatch)
    api = Api()
    api.window = _FakeWindow(None)
    assert api.pick_folder() is None
