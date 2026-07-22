"""Native desktop shell: a pywebview window around the operational dashboard.

Kept separate from ``dashboard/`` because it depends on the optional ``desktop`` extra
(``pywebview``), which most installs (server/CLI-only, or the plain browser GUI) never need.
"""

from __future__ import annotations


class Api:
    """JS-bridge object exposed to the page as ``window.pywebview.api``.

    ``window`` is set by the caller right after ``webview.create_window(...)`` returns (pywebview
    has no way to hand a window its own reference at construction time). Kept dependency-injectable
    — a test can set ``.window`` to a stub — so this is unit-testable without pywebview installed.
    """

    def __init__(self) -> None:
        self.window = None

    def pick_folder(self) -> str | None:
        if self.window is None:
            return None
        import webview

        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None
