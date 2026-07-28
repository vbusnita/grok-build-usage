"""macOS menu bar app: toggle HUD, refresh usage, open Grok billing page.

Threading model matches ara-agent:
  - rumps owns the main (AppKit) thread
  - billing HTTP runs on a daemon worker; results marshalled back via
    rumps.Timer + a simple pending field (atomic under CPython)
"""

from __future__ import annotations

import logging
import subprocess
import threading
import webbrowser
from typing import Optional

import rumps

from gbu.billing import fetch_snapshot
from gbu.hud import UsageHUD
from gbu.models import UsageSnapshot

log = logging.getLogger(__name__)

POLL_SECONDS = 45
USAGE_URL = "https://grok.com/?_s=usage"
DEFAULT_TITLE = "GBU"


class GrokBuildUsageApp(rumps.App):
    def __init__(self, *, poll_seconds: float = POLL_SECONDS, start_hud_visible: bool = True):
        super().__init__(
            name="Grok Build Usage",
            title=DEFAULT_TITLE,
            quit_button=None,
        )
        self.poll_seconds = poll_seconds
        self._snapshot: Optional[UsageSnapshot] = None
        self._pending: Optional[UsageSnapshot] = None
        self._fetching = False
        self._lock = threading.Lock()

        self._hud = UsageHUD.alloc().init()
        self._hud_visible = start_hud_visible
        if start_hud_visible:
            self._hud.show()
        else:
            self._hud.hide()

        self.menu = [
            rumps.MenuItem("Hide Overlay", callback=self._toggle_overlay),
            rumps.MenuItem("Refresh Now", callback=self._refresh_now),
            None,
            rumps.MenuItem("Open Grok Usage…", callback=self._open_usage_page),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]
        self._toggle_item = self.menu["Hide Overlay"]

        # Apply UI updates + kick background polls on the main thread.
        self._ui_timer = rumps.Timer(self._on_tick, 0.5)
        self._ui_timer.start()
        self._poll_timer = rumps.Timer(self._on_poll, max(5.0, float(poll_seconds)))
        self._poll_timer.start()

        # Immediate first fetch
        self._kick_fetch()

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------

    def _toggle_overlay(self, _sender=None):
        visible = self._hud.toggle()
        self._hud_visible = visible
        self._toggle_item.title = "Hide Overlay" if visible else "Show Overlay"

    def _refresh_now(self, _sender=None):
        self._kick_fetch(force=True)

    def _open_usage_page(self, _sender=None):
        try:
            webbrowser.open(USAGE_URL)
        except Exception:
            subprocess.run(["open", USAGE_URL], check=False)

    def _quit(self, _sender=None):
        try:
            self._hud.hide()
        except Exception:
            pass
        rumps.quit_application()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _on_poll(self, _sender):
        self._kick_fetch()

    def _on_tick(self, _sender):
        pending = None
        with self._lock:
            if self._pending is not None:
                pending = self._pending
                self._pending = None
        if pending is None:
            return
        self._snapshot = pending
        self.title = pending.menu_title()
        try:
            self._hud.update_snapshot(pending)
        except Exception:
            log.exception("HUD update failed")

    def _kick_fetch(self, force: bool = False):
        with self._lock:
            if self._fetching and not force:
                return
            self._fetching = True

        def worker():
            try:
                snap = fetch_snapshot()
            except Exception as exc:  # noqa: BLE001
                log.exception("fetch failed")
                from gbu.models import snapshot_from_billing

                snap = snapshot_from_billing({}, error=f"Fetch failed: {type(exc).__name__}")
            with self._lock:
                self._pending = snap
                self._fetching = False

        threading.Thread(target=worker, name="gbu-billing", daemon=True).start()


def run_app(*, poll_seconds: float = POLL_SECONDS, start_hud_visible: bool = True) -> None:
    """Launch the menu bar app (blocks)."""
    # Accessory policy so we don't bounce a Dock icon — menu bar only.
    try:
        from AppKit import NSApp, NSApplicationActivationPolicyAccessory
        from PyObjCTools import AppHelper  # noqa: F401 — ensure AppKit ready

        # rumps creates NSApp; set policy after first access
        app = GrokBuildUsageApp(
            poll_seconds=poll_seconds,
            start_hud_visible=start_hud_visible,
        )
        # Best-effort: hide from Dock if possible
        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        app.run()
    except Exception:
        # Fallback without policy tweak
        GrokBuildUsageApp(
            poll_seconds=poll_seconds,
            start_hud_visible=start_hud_visible,
        ).run()
