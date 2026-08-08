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

Persistence:
  The icon must stay visible until the user chooses Quit. AppKit can still
  park the status item (sleep/wake, display changes, menu-bar overflow).
  We continuously self-heal; if repair fails repeatedly we exit non-zero so
  the LaunchAgent (KeepAlive SuccessfulExit=false) restarts us. Menu Quit
  exits 0 so launchd does not bring us back.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
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

# Status-item health watch
STATUS_WATCH_FAST_S = 0.75
STATUS_WATCH_SLOW_S = 15.0
# Grace period after launch / recreate before we tear the item down again.
STATUS_RECREATE_GRACE_S = 8.0
# Min spacing between recreate attempts (avoid thrashing AppKit).
STATUS_RECREATE_COOLDOWN_S = 20.0
# Consecutive unhealthy ticks (at current watch interval) before recreate.
STATUS_UNHEALTHY_BEFORE_RECREATE = 3
# After this many failed heal cycles in a row, exit non-zero → LaunchAgent restart.
STATUS_MAX_FAILED_HEALS = 4


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
        self._want_hud = start_hud_visible
        self._hud_shown_once = False
        self._status_slowed = False
        self._ever_healthy = False
        self._unhealthy_streak = 0
        self._failed_heals = 0
        self._last_recreate_at = 0.0
        self._started_at = time.monotonic()
        self._user_quit = False
        # Lazily construct the floating HUD only after the status item is
        # healthy. Creating/showing an NSPanel during status-item layout
        # leaves the menu-bar button at height 0 permanently on recent macOS.
        self._hud: Optional[UsageHUD] = None
        self._hud_visible = False

        self.menu = [
            rumps.MenuItem("Show Overlay", callback=self._toggle_overlay),
            rumps.MenuItem("Refresh Now", callback=self._refresh_now),
            None,
            rumps.MenuItem("Open Grok Usage…", callback=self._open_usage_page),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]
        self._toggle_item = self.menu["Show Overlay"]

        # Apply UI updates + kick background polls on the main thread.
        self._ui_timer = rumps.Timer(self._on_tick, 0.5)
        self._ui_timer.start()
        self._poll_timer = rumps.Timer(self._on_poll, max(5.0, float(poll_seconds)))
        self._poll_timer.start()
        # Status item only exists after app.run(); fix appearance once it does.
        self._status_timer = rumps.Timer(self._on_status_watch, STATUS_WATCH_FAST_S)
        self._status_timer.start()
        self._install_system_observers()

        # Immediate first fetch
        self._kick_fetch()

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------

    def _ensure_hud(self) -> UsageHUD:
        if self._hud is None:
            self._hud = UsageHUD.alloc().init()
            self._hud.hide()
        return self._hud

    def _toggle_overlay(self, _sender=None):
        hud = self._ensure_hud()
        if not self._hud_visible:
            hud.show()
            self._hud_visible = True
            self._want_hud = True
            self._hud_shown_once = True
            self._toggle_item.title = "Hide Overlay"
            if self._snapshot is not None:
                hud.update_snapshot(self._snapshot)
        else:
            hud.hide()
            self._hud_visible = False
            self._want_hud = False
            self._toggle_item.title = "Show Overlay"

    def _refresh_now(self, _sender=None):
        self._kick_fetch(force=True)

    def _open_usage_page(self, _sender=None):
        try:
            webbrowser.open(USAGE_URL)
        except Exception:
            subprocess.run(["open", USAGE_URL], check=False)

    def _quit(self, _sender=None):
        # Mark intentional quit so KeepAlive (SuccessfulExit=false) does not
        # relaunch us. NSApplication.terminate_ exits 0.
        self._user_quit = True
        log.info("user quit from menu bar — exiting cleanly (no auto-restart)")
        try:
            if self._hud is not None:
                self._hud.hide()
        except Exception:
            pass
        rumps.quit_application()

    # ------------------------------------------------------------------
    # Status item (menu bar) — modern button API + continuous self-heal
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
                return float(frame.size.height) >= 20.0 and float(frame.origin.x) >= -1.0

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

    def _set_status_watch_interval(self, interval: float, *, slowed: bool) -> None:
        """Replace the status watch timer interval (must run on main thread)."""
        if self._status_slowed == slowed:
            return
        try:
            self._status_timer.stop()
        except Exception:
            pass
        self._status_timer = rumps.Timer(self._on_status_watch, interval)
        self._status_timer.start()
        self._status_slowed = slowed

    def _install_system_observers(self) -> None:
        """Re-check / repair after sleep and display topology changes."""
        try:
            from AppKit import NSWorkspace
            from Foundation import NSNotificationCenter, NSObject
            from PyObjCTools import AppHelper

            app = self

            class _GBUObservers(NSObject):
                def workspaceDidWake_(self, _note):
                    log.info("system wake — scheduling status repair")
                    AppHelper.callAfter(app._on_system_layout_change)

                def screenParametersChanged_(self, _note):
                    log.info("screen parameters changed — scheduling status repair")
                    AppHelper.callAfter(app._on_system_layout_change)

            self._observer_proxy = _GBUObservers.alloc().init()
            wsnc = NSWorkspace.sharedWorkspace().notificationCenter()
            wsnc.addObserver_selector_name_object_(
                self._observer_proxy,
                "workspaceDidWake:",
                "NSWorkspaceDidWakeNotification",
                None,
            )
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._observer_proxy,
                "screenParametersChanged:",
                "NSApplicationDidChangeScreenParametersNotification",
                None,
            )
            log.info("installed wake + screen observers for status self-heal")
        except Exception:
            log.exception("failed to install system observers")

    def _on_system_layout_change(self) -> None:
        """Force a fast re-check after sleep/display change (main thread)."""
        if self._user_quit:
            return
        self._unhealthy_streak = STATUS_UNHEALTHY_BEFORE_RECREATE  # act soon
        self._set_status_watch_interval(STATUS_WATCH_FAST_S, slowed=False)
        # Give AppKit a beat to re-layout the menu bar, then heal if needed.
        try:
            from PyObjCTools import AppHelper

            AppHelper.callLater(1.5, self._heal_if_unhealthy)
        except Exception:
            self._heal_if_unhealthy()

    def _heal_if_unhealthy(self) -> None:
        if self._user_quit:
            return
        if self._status_frame_healthy():
            self._unhealthy_streak = 0
            return
        log.warning("status item unhealthy after layout change — recreating")
        self._recreate_status_item()

    def _recreate_status_item(self) -> bool:
        """Tear down and re-create the NSStatusItem. Returns True if created."""
        nsapp = getattr(self, "_nsapp", None)
        old = self._status_item()
        if nsapp is None:
            return False
        try:
            from AppKit import NSStatusBar, NSVariableStatusItemLength

            bar = NSStatusBar.systemStatusBar()
            menu = old.menu() if old is not None else None
            if old is not None:
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
            else:
                # rumps keeps the NSMenu on the App; re-bind if needed.
                try:
                    if getattr(nsapp, "_menu", None) is not None:
                        new.setMenu_(nsapp._menu)
                except Exception:
                    pass
            try:
                new.setVisible_(True)
            except Exception:
                pass
            nsapp.nsstatusitem = new
            self._last_recreate_at = time.monotonic()
            self._unhealthy_streak = 0
            self._apply_status_appearance(self._title or DEFAULT_TITLE)
            log.warning("recreated status item (was unhealthy)")
            return True
        except Exception:
            log.exception("status item recreate failed")
            return False

    def _escalate_restart(self, reason: str) -> None:
        """Exit non-zero so LaunchAgent KeepAlive restarts the process.

        Menu Quit uses exit 0 and must never call this.
        """
        if self._user_quit:
            return
        log.error(
            "status item unrecoverable (%s) — exiting 1 for LaunchAgent restart",
            reason,
        )
        try:
            if self._hud is not None:
                self._hud.hide()
        except Exception:
            pass
        # Hard exit: avoid AppKit terminate_ (exit 0) so KeepAlive fires.
        sys.exit(1)

    def _reveal_hud_if_wanted(self) -> None:
        """Show deferred floating overlay once the status item is stable."""
        if self._hud_shown_once or not self._want_hud:
            return
        self._hud_shown_once = True
        try:
            hud = self._ensure_hud()
            hud.show()
            self._hud_visible = True
            self._toggle_item.title = "Hide Overlay"
            if self._snapshot is not None:
                hud.update_snapshot(self._snapshot)
            log.info("HUD shown after status item became healthy")
        except Exception:
            log.exception("deferred HUD show failed")

    def _on_status_watch(self, _sender):
        if self._user_quit:
            return

        # Status item is created inside App.run() after __init__; wait for it.
        item = self._status_item()
        if item is None:
            # rumps has not wired the item yet — keep waiting on fast timer.
            return

        self._status_checks += 1
        try:
            item.setVisible_(True)
        except Exception:
            pass
        self._apply_status_appearance(self._title or DEFAULT_TITLE)

        healthy = self._status_frame_healthy()
        now = time.monotonic()
        in_grace = (now - self._started_at) < STATUS_RECREATE_GRACE_S or (
            self._last_recreate_at > 0
            and (now - self._last_recreate_at) < STATUS_RECREATE_GRACE_S
        )

        if healthy:
            if not self._ever_healthy:
                log.info("status item healthy for the first time")
            self._ever_healthy = True
            self._unhealthy_streak = 0
            self._failed_heals = 0
            self._reveal_hud_if_wanted()
            self._set_status_watch_interval(STATUS_WATCH_SLOW_S, slowed=True)
        else:
            self._unhealthy_streak += 1
            # While broken, poll fast so we recover quickly after wake/layout.
            self._set_status_watch_interval(STATUS_WATCH_FAST_S, slowed=False)

            cooldown_ok = (
                self._last_recreate_at == 0.0
                or (now - self._last_recreate_at) >= STATUS_RECREATE_COOLDOWN_S
            )
            can_recreate = (
                not in_grace
                and self._unhealthy_streak >= STATUS_UNHEALTHY_BEFORE_RECREATE
                and cooldown_ok
            )
            if can_recreate:
                # A prior recreate that never restored health counts as a failed heal.
                if self._last_recreate_at > 0:
                    self._failed_heals += 1
                    log.warning(
                        "previous status heal did not stick (failed_heals=%s)",
                        self._failed_heals,
                    )
                    if self._failed_heals >= STATUS_MAX_FAILED_HEALS:
                        self._escalate_restart(
                            f"{self._failed_heals} failed status-item heal cycles"
                        )
                        return

                log.warning(
                    "status item unhealthy (check %s, streak %s) — recreating",
                    self._status_checks,
                    self._unhealthy_streak,
                )
                if not self._recreate_status_item():
                    self._failed_heals += 1
                    if self._failed_heals >= STATUS_MAX_FAILED_HEALS:
                        self._escalate_restart(
                            f"{self._failed_heals} failed status-item heal cycles"
                        )
                        return

        # Sparse logging: first few ticks, transitions, and occasional heartbeat.
        log_this = (
            self._status_checks <= 6
            or self._status_checks % 40 == 0
            or (
                not healthy
                and self._unhealthy_streak in (1, STATUS_UNHEALTHY_BEFORE_RECREATE)
            )
        )
        if log_this:
            btn = self._status_button()
            try:
                win = btn.window() if btn is not None else None
                log.info(
                    "status watch #%s healthy=%s title=%r streak=%s failed_heals=%s frame=%s",
                    self._status_checks,
                    healthy,
                    self._title,
                    self._unhealthy_streak,
                    self._failed_heals,
                    win.frame() if win is not None else None,
                )
            except Exception:
                pass

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
        if self._hud is not None:
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
