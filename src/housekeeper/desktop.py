"""Native desktop shell: a pywebview window around the operational dashboard.

Separate from ``dashboard/`` — depends on the optional ``desktop`` extra most installs skip.
"""

from __future__ import annotations


class Api:
    """JS bridge as ``window.pywebview.api``. Caller sets ``.window`` after create_window."""

    def __init__(self) -> None:
        self.window = None

    def pick_folder(self) -> str | None:
        if self.window is None:
            return None
        import webview

        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None
