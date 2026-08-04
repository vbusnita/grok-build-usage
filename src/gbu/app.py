"""macOS menu bar app: toggle HUD, refresh usage, open Grok billing page.

Threading model matches ara-agent:
  - rumps owns the main (AppKit) thread
  - billing HTTP runs on a daemon worker; results marshalled back via
    rumps.Timer + a simple pending field (atomic under CPython)

Status-item notes (macOS 14+ / 26):
  rumps still uses the deprecated NSStatusItem.setTitle_/setImage_ APIs.
  On newer systems the painted surface is NSStatusBarButton — we always
  push title/icon through button() and repair the item if AppKit parks
  its window off-screen (a real failure mode we hit in the wild).
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
# SF Symbol — template so it inverts correctly in light/dark menu bars
STATUS_SYMBOL = "chart.bar.fill"


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
        self._status_checks = 0

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
        # Status item only exists after app.run(); fix appearance once it does.
        self._status_timer = rumps.Timer(self._on_status_watch, 0.75)
        self._status_timer.start()

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
    # Status item (menu bar) — modern button API + self-heal
    # ------------------------------------------------------------------

    def _status_item(self):
        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None:
            return None
        return getattr(nsapp, "nsstatusitem", None)

    def _status_button(self):
        item = self._status_item()
        if item is None:
            return None
        try:
            return item.button()
        except Exception:
            return None

    def _apply_status_appearance(self, title: Optional[str] = None) -> bool:
        """Push title + template SF Symbol through NSStatusBarButton.

        Returns True if a button was available and updated.
        """
        btn = self._status_button()
        if btn is None:
            return False

        text = title if title is not None else (self._title or DEFAULT_TITLE)
        if text is None:
            text = DEFAULT_TITLE

        try:
            btn.setTitle_(str(text))
        except Exception:
            log.exception("status button setTitle failed")

        try:
            from AppKit import NSImage, NSImageLeft

            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                STATUS_SYMBOL, "Grok Build Usage"
            )
            if img is not None:
                img.setTemplate_(True)
                btn.setImage_(img)
                try:
                    btn.setImagePosition_(NSImageLeft)
                except Exception:
                    # NSImageLeft may not bind on every PyObjC; 2 == NSImageLeft
                    try:
                        btn.setImagePosition_(2)
                    except Exception:
                        pass
        except Exception:
            log.exception("status button setImage failed")

        try:
            btn.setToolTip_("Grok Build Usage — click for menu")
        except Exception:
            pass

        # Also keep the legacy path in sync (rumps / accessibility).
        item = self._status_item()
        if item is not None:
            try:
                item.setTitle_(str(text))
            except Exception:
                pass

        return True

    def _status_frame_healthy(self) -> bool:
        """True if the status item window is on-screen in the menu bar strip."""
        btn = self._status_button()
        if btn is None:
            return False
        try:
            win = btn.window()
            if win is None:
                return False
            frame = win.frame()
            # AppKit: origin bottom-left. Menu bar windows sit near top of screen.
            from AppKit import NSScreen

            screen = NSScreen.mainScreen()
            if screen is None:
                return frame.size.height > 0 and frame.origin.x >= 0

            sh = float(screen.frame().size.height)
            # Healthy: non-zero height, x on-screen, y in upper half (menu bar).
            return (
                float(frame.size.height) >= 20.0
                and float(frame.origin.x) >= -1.0
                and float(frame.origin.y) > sh * 0.5
            )
        except Exception:
            log.exception("status frame check failed")
            return False

    def _recreate_status_item(self) -> None:
        """Tear down and re-create the NSStatusItem (recovers off-screen parking)."""
        nsapp = getattr(self, "_nsapp", None)
        old = self._status_item()
        if nsapp is None or old is None:
            return
        try:
            from AppKit import NSStatusBar, NSVariableStatusItemLength

            bar = NSStatusBar.systemStatusBar()
            menu = old.menu()
            try:
                bar.removeStatusItem_(old)
            except Exception:
                log.exception("removeStatusItem failed")

            try:
                length = NSVariableStatusItemLength
            except Exception:
                length = -1.0

            new = bar.statusItemWithLength_(length)
            try:
                new.setHighlightMode_(True)
            except Exception:
                pass
            if menu is not None:
                new.setMenu_(menu)
            nsapp.nsstatusitem = new
            self._apply_status_appearance(self._title or DEFAULT_TITLE)
            log.warning("recreated status item (was unhealthy)")
        except Exception:
            log.exception("status item recreate failed")

    def _on_status_watch(self, _sender):
        # Status item is created inside App.run() after __init__; wait for it.
        if self._status_item() is None:
            return

        self._status_checks += 1
        self._apply_status_appearance(self._title or DEFAULT_TITLE)

        healthy = self._status_frame_healthy()
        # AppKit often reports height=0 for the first couple of ticks while the
        # status window is laid out — wait before tearing it down.
        if not healthy and self._status_checks in (10, 16, 24):
            log.warning(
                "status item unhealthy (check %s) — recreating",
                self._status_checks,
            )
            self._recreate_status_item()
            healthy = self._status_frame_healthy()

        if self._status_checks <= 6 or self._status_checks % 40 == 0:
            btn = self._status_button()
            try:
                win = btn.window() if btn is not None else None
                log.info(
                    "status watch #%s healthy=%s title=%r frame=%s",
                    self._status_checks,
                    healthy,
                    self._title,
                    win.frame() if win is not None else None,
                )
            except Exception:
                pass

        # After a few successful healthy checks, slow down (reuse poll timer cadence).
        if healthy and self._status_checks >= 6:
            try:
                self._status_timer.stop()
            except Exception:
                pass
            self._status_timer = rumps.Timer(self._on_status_watch, 30.0)
            self._status_timer.start()

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
        title = pending.menu_title()
        self.title = title
        self._apply_status_appearance(title)
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Prefer a human name in Activity Monitor / some System Settings rows.
    # (Control Center "Allow in the Menu Bar" still keys off code identity —
    # install-app.sh ad-hoc signs the runtime as app.grokbuild.usage.)
    try:
        from Foundation import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_("Grok Build Usage")
    except Exception:
        log.debug("setProcessName failed", exc_info=True)

    # Accessory policy BEFORE rumps builds the status item. Must use
    # NSApplication.sharedApplication() — bare NSApp is often None here,
    # and a silent failure was leaving the status item in a bad state.
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        ns = NSApplication.sharedApplication()
        ns.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        log.info("activation policy set to accessory (%s)", ns.activationPolicy())
    except Exception:
        log.exception("failed to set activation policy")

    GrokBuildUsageApp(
        poll_seconds=poll_seconds,
        start_hud_visible=start_hud_visible,
    ).run()
