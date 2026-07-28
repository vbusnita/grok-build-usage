"""Floating Grok Build usage HUD — no chrome.

No card, no glass, no border: title, progress bar, and metric groups sit
directly on the desktop as translucent-free floating UI.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightMedium,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageSymbolConfiguration,
    NSImageView,
    NSKernAttributeName,
    NSPanel,
    NSShadow,
    NSShadowAttributeName,
    NSStatusWindowLevel,
    NSTextField,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import (
    NSAttributedString,
    NSDictionary,
    NSObject,
    NSPoint,
    NSRect,
    NSSize,
    NSUserDefaults,
)

from gbu.models import UsageSnapshot

log = logging.getLogger(__name__)

# ── Type / colour (Lyra text tokens, no surfaces) ────────────────────────────
_TEXT = (200 / 255, 200 / 255, 210 / 255, 1.0)
_TELEM_LABEL = (180 / 255, 180 / 255, 190 / 255, 0.88)
_TELEM_VALUE = (240 / 255, 240 / 255, 245 / 255, 0.98)
_TEXT_MUTED = (160 / 255, 160 / 255, 170 / 255, 0.85)
_ICON_GLYPH = (180 / 255, 180 / 255, 190 / 255, 0.95)

_GREEN = (74 / 255, 222 / 255, 128 / 255, 1.0)   # brighter for bare desktop
_YELLOW = (250 / 255, 204 / 255, 21 / 255, 1.0)
_RED = (248 / 255, 113 / 255, 113 / 255, 1.0)
_TRACK = (1.0, 1.0, 1.0, 0.22)
_SHADOW = (0.0, 0.0, 0.0, 0.55)

# Layout — scaled up for glanceability at arm’s length
HUD_WIDTH = 480.0
PAD_X = 6.0
PAD_Y = 6.0
BAR_H = 8.0
ICON_PT = 32.0
TITLE_SIZE = 28.0
PCT_SIZE = 26.0
LABEL_SIZE = 13.0
VALUE_SIZE = 18.0
METRIC_GAP = 22.0
METRIC_BLOCK_H = 44.0
FRAME_KEY = "gbu.hudLastOrigin"
_TOOLS_SYMBOL = "wrench.and.screwdriver"


def _ns_color(rgba: Tuple[float, float, float, float]) -> NSColor:
    r, g, b, a = rgba
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


_CG_CACHE: dict = {}


def _cg_color(rgba: Tuple[float, float, float, float]):
    key = tuple(rgba)
    cg = _CG_CACHE.get(key)
    if cg is None:
        cg = _ns_color(rgba).CGColor()
        _CG_CACHE[key] = cg
    return cg


_LABEL_STYLE: dict[int, dict] = {}


def _text_shadow() -> NSShadow:
    """Soft drop shadow so white type reads on any desktop wallpaper."""
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(_ns_color(_SHADOW))
    shadow.setShadowOffset_(NSSize(0, -1))
    shadow.setShadowBlurRadius_(3.0)
    return shadow


def _make_text_field(
    x: float,
    y: float,
    w: float,
    h: float,
    size: float = 14,
    weight=NSFontWeightRegular,
    color=_TEXT,
    align: str = "left",
    mono: bool = True,
    kern: float = 0.0,
) -> NSTextField:
    lab = NSTextField.alloc().initWithFrame_(NSRect(NSPoint(x, y), NSSize(w, h)))
    lab.setBezeled_(False)
    lab.setDrawsBackground_(False)
    lab.setEditable_(False)
    lab.setSelectable_(False)
    if mono:
        font = NSFont.monospacedSystemFontOfSize_weight_(size, weight)
    else:
        font = NSFont.systemFontOfSize_weight_(size, weight)
    lab.setFont_(font)
    lab.setTextColor_(_ns_color(color))
    lab.setStringValue_("")
    if align == "right":
        lab.setAlignment_(2)
    _LABEL_STYLE[id(lab)] = {"font": font, "color": color, "kern": kern}
    return lab


def _set_text(lab: NSTextField, text: str, color=None, kern: Optional[float] = None) -> None:
    style = _LABEL_STYLE.get(id(lab), {})
    font = style.get("font") or lab.font()
    rgba = color if color is not None else style.get("color", _TEXT)
    k = kern if kern is not None else style.get("kern", 0.0)
    if color is not None:
        style = dict(style)
        style["color"] = color
        _LABEL_STYLE[id(lab)] = style
    attrs = NSDictionary.dictionaryWithObjects_forKeys_(
        [font, _ns_color(rgba), float(k), _text_shadow()],
        [
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSKernAttributeName,
            NSShadowAttributeName,
        ],
    )
    lab.setAttributedStringValue_(
        NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    )


def _tools_image(point_size: float = 18.0) -> Optional[NSImage]:
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        _TOOLS_SYMBOL, "Grok Build tools"
    )
    if img is None:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "hammer.fill", "Grok Build tools"
        )
    if img is None:
        return None
    try:
        cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            point_size, NSFontWeightMedium, 1
        )
        configured = img.imageWithSymbolConfiguration_(cfg)
        if configured is not None:
            img = configured
    except Exception:
        pass
    img.setTemplate_(True)
    return img


class UsageHUD(NSObject):
    """Chrome-free floating usage HUD."""

    def init(self):
        self = objc.super(UsageHUD, self).init()
        if self is None:
            return None
        self._snapshot: Optional[UsageSnapshot] = None
        self._metric_views: List[NSView] = []
        self._build_panel()
        self._restore_position()
        self._apply_loading()
        return self

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_snapshot(self, snapshot: Optional[UsageSnapshot]) -> None:
        self._snapshot = snapshot
        if snapshot is None:
            self._apply_loading()
            return
        self._apply_snapshot(snapshot)

    def show(self) -> None:
        self._panel.orderFront_(None)

    def hide(self) -> None:
        self._persist_position()
        self._panel.orderOut_(None)

    def isVisible(self) -> bool:  # noqa: N802
        return bool(self._panel.isVisible())

    def toggle(self) -> bool:
        if self.isVisible():
            self.hide()
            return False
        self.show()
        return True

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_panel(self) -> None:
        height = self._height_for_metrics(3)
        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSRect(NSPoint(40, 80), NSSize(HUD_WIDTH, height)),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        self._panel.setLevel_(NSStatusWindowLevel)
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(NSColor.clearColor())
        self._panel.setHasShadow_(False)  # text carries its own shadow
        self._panel.setMovableByWindowBackground_(True)
        self._panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._panel.setHidesOnDeactivate_(False)
        # Fully clear panel — no dimming
        try:
            self._panel.setAlphaValue_(1.0)
        except Exception:
            pass

        content = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, height))
        )
        content.setWantsLayer_(True)
        content.layer().setBackgroundColor_(_cg_color((0, 0, 0, 0)))
        content.layer().setOpaque_(False)
        self._panel.setContentView_(content)
        self._content = content

        # Icon (no plate)
        self._icon_view = NSImageView.alloc().initWithFrame_(
            NSRect(NSPoint(PAD_X, height - PAD_Y - ICON_PT), NSSize(ICON_PT, ICON_PT))
        )
        self._icon_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        tools = _tools_image(22.0)
        if tools is not None:
            self._icon_view.setImage_(tools)
        try:
            self._icon_view.setContentTintColor_(_ns_color(_ICON_GLYPH))
        except Exception:
            pass
        content.addSubview_(self._icon_view)

        # Title
        self._brand = _make_text_field(
            PAD_X + ICON_PT + 12,
            height - PAD_Y - 32,
            HUD_WIDTH - ICON_PT - 96,
            34,
            size=TITLE_SIZE,
            weight=NSFontWeightSemibold,
            color=_TEXT,
            mono=False,
            kern=-0.45,
        )
        _set_text(self._brand, "Grok Build")
        content.addSubview_(self._brand)

        # LIVE
        self._live = _make_text_field(
            HUD_WIDTH - 68,
            height - PAD_Y - 24,
            64,
            18,
            size=14,
            weight=NSFontWeightMedium,
            color=_GREEN,
            align="right",
            mono=True,
            kern=0.9,
        )
        _set_text(self._live, "LIVE")
        content.addSubview_(self._live)

        # Progress bar track (minimal — only the fill is “content”)
        track_x = PAD_X
        track_w = HUD_WIDTH - 2 * PAD_X - 78
        track_y = height - PAD_Y - ICON_PT - 28
        self._track = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(track_x, track_y), NSSize(track_w, BAR_H))
        )
        self._track.setWantsLayer_(True)
        self._track.layer().setCornerRadius_(BAR_H / 2)
        self._track.layer().setMasksToBounds_(True)
        self._track.layer().setBackgroundColor_(_cg_color(_TRACK))
        content.addSubview_(self._track)

        self._fill = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), NSSize(0, BAR_H))
        )
        self._fill.setWantsLayer_(True)
        self._fill.layer().setCornerRadius_(BAR_H / 2)
        self._fill.layer().setBackgroundColor_(_cg_color(_GREEN))
        self._track.addSubview_(self._fill)
        self._track_w = track_w

        # Big %
        self._gauge_pct = _make_text_field(
            HUD_WIDTH - PAD_X - 74,
            track_y - 10,
            74,
            30,
            size=PCT_SIZE,
            weight=NSFontWeightSemibold,
            color=_TELEM_VALUE,
            align="right",
            mono=True,
        )
        _set_text(self._gauge_pct, "—%")
        content.addSubview_(self._gauge_pct)

        # Limit caption
        self._gauge_label = _make_text_field(
            track_x,
            track_y - 22,
            240,
            16,
            size=LABEL_SIZE,
            weight=NSFontWeightMedium,
            color=_TELEM_LABEL,
            mono=True,
            kern=0.9,
        )
        _set_text(self._gauge_label, "WEEKLY LIMIT")
        content.addSubview_(self._gauge_label)

        self._metrics_host = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, METRIC_BLOCK_H))
        )
        content.addSubview_(self._metrics_host)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _height_for_metrics(self, n: int) -> float:
        header = ICON_PT + 12
        bar = 48
        metrics = METRIC_BLOCK_H + 8 if n > 0 else 0
        return PAD_Y + header + bar + metrics + PAD_Y

    def _resize_to(self, height: float) -> None:
        frame = self._panel.frame()
        new_y = frame.origin.y + (frame.size.height - height)
        self._panel.setFrame_display_(
            NSRect(NSPoint(frame.origin.x, new_y), NSSize(HUD_WIDTH, height)),
            True,
        )
        self._content.setFrame_(NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, height)))

        self._icon_view.setFrame_(
            NSRect(
                NSPoint(PAD_X, height - PAD_Y - ICON_PT),
                NSSize(ICON_PT, ICON_PT),
            )
        )
        self._brand.setFrame_(
            NSRect(
                NSPoint(PAD_X + ICON_PT + 12, height - PAD_Y - 32),
                NSSize(HUD_WIDTH - ICON_PT - 96, 34),
            )
        )
        self._live.setFrame_(
            NSRect(NSPoint(HUD_WIDTH - 68, height - PAD_Y - 24), NSSize(64, 18))
        )

        track_x = PAD_X
        track_w = HUD_WIDTH - 2 * PAD_X - 78
        track_y = height - PAD_Y - ICON_PT - 28
        self._track_w = track_w
        self._track.setFrame_(NSRect(NSPoint(track_x, track_y), NSSize(track_w, BAR_H)))
        self._gauge_pct.setFrame_(
            NSRect(NSPoint(HUD_WIDTH - PAD_X - 74, track_y - 10), NSSize(74, 30))
        )
        self._gauge_label.setFrame_(
            NSRect(NSPoint(track_x, track_y - 22), NSSize(240, 16))
        )

    def _set_level(self, level: str) -> None:
        if level == "error":
            fill, live_color, live_text = _RED, _RED, "ERR"
        elif level == "bad":
            fill, live_color, live_text = _RED, _RED, "HIGH"
        elif level == "warn":
            fill, live_color, live_text = _YELLOW, _YELLOW, "LIVE"
        else:
            fill, live_color, live_text = _GREEN, _GREEN, "LIVE"
        self._fill.layer().setBackgroundColor_(_cg_color(fill))
        _set_text(self._live, live_text, color=live_color, kern=0.8)

    def _set_bar(self, pct: float) -> None:
        width = max(0.0, min(1.0, pct / 100.0)) * self._track_w
        if pct > 0 and width < 6:
            width = 6
        self._fill.setFrame_(NSRect(NSPoint(0, 0), NSSize(width, BAR_H)))

    # ------------------------------------------------------------------
    # Floating metric groups (text only — no pill chrome)
    # ------------------------------------------------------------------

    def _clear_metrics(self) -> None:
        for v in self._metric_views:
            v.removeFromSuperview()
        self._metric_views = []

    def _render_metrics(self, rows: Sequence[Tuple[str, str]], height: float) -> None:
        self._clear_metrics()
        if not rows:
            self._metrics_host.setFrame_(NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, 1)))
            return

        rows = list(rows)[:4]
        n = len(rows)
        host_y = PAD_Y
        self._metrics_host.setFrame_(
            NSRect(NSPoint(0, host_y), NSSize(HUD_WIDTH, METRIC_BLOCK_H))
        )

        # Equal columns across the width
        col_w = (HUD_WIDTH - 2 * PAD_X - METRIC_GAP * (n - 1)) / n
        x = PAD_X
        for label, value in rows:
            block = NSView.alloc().initWithFrame_(
                NSRect(NSPoint(x, 0), NSSize(col_w, METRIC_BLOCK_H))
            )
            # No background — pure floating text
            lab = _make_text_field(
                0,
                METRIC_BLOCK_H - 16,
                col_w,
                16,
                size=LABEL_SIZE,
                weight=NSFontWeightMedium,
                color=_TELEM_LABEL,
                mono=True,
                kern=0.8,
            )
            _set_text(lab, label)
            val = _make_text_field(
                0,
                2,
                col_w,
                22,
                size=VALUE_SIZE,
                weight=NSFontWeightSemibold,
                color=_TELEM_VALUE,
                mono=True,
            )
            # Show full value — columns are wide enough at 400px for 3 metrics
            _set_text(val, value)
            block.addSubview_(lab)
            block.addSubview_(val)
            self._metrics_host.addSubview_(block)
            self._metric_views.append(block)
            x += col_w + METRIC_GAP

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _apply_loading(self) -> None:
        rows = [("STATUS", "…")]
        height = self._height_for_metrics(len(rows))
        self._resize_to(height)
        self._set_level("ok")
        self._set_bar(0)
        _set_text(self._gauge_label, "USAGE", color=_TELEM_LABEL, kern=0.8)
        _set_text(self._gauge_pct, "—%", color=_TEXT_MUTED)
        _set_text(self._live, "…", color=_TEXT_MUTED, kern=0.8)
        self._render_metrics(rows, height)

    def _apply_snapshot(self, snap: UsageSnapshot) -> None:
        rows = snap.metric_rows()
        preferred: List[Tuple[str, str]] = []
        for key in (
            "RESET",
            "CREDITS",
            "AUTO TOPUP",
            "PAYG",
            "TIER",
            "STATUS",
            "ERROR",
            "HINT",
        ):
            for lab, val in rows:
                if lab == key and (lab, val) not in preferred:
                    preferred.append((lab, val))
        if not preferred:
            preferred = list(rows)

        n = min(4, max(1, len(preferred)))
        height = self._height_for_metrics(n)
        self._resize_to(height)
        level = snap.gauge_level()
        self._set_level(level)

        if snap.error:
            _set_text(self._gauge_label, "USAGE", color=_TELEM_LABEL, kern=0.8)
            _set_text(self._gauge_pct, "?", color=_RED)
            self._set_bar(0)
            err = snap.error if len(snap.error) <= 28 else snap.error[:27] + "…"
            preferred = [("ERROR", err), ("HINT", "grok login")]
            height = self._height_for_metrics(len(preferred))
            self._resize_to(height)
            self._render_metrics(preferred, height)
            return

        _set_text(
            self._gauge_label,
            snap.usage_label.upper(),
            color=_TELEM_LABEL,
            kern=0.8,
        )
        pct_color = _TELEM_VALUE
        if level == "warn":
            pct_color = _YELLOW
        elif level == "bad":
            pct_color = _RED
        _set_text(self._gauge_pct, f"{snap.usage_pct_display}%", color=pct_color)
        self._set_bar(snap.usage_pct)
        self._render_metrics(preferred[:4], height)

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    def _persist_position(self) -> None:
        origin = self._panel.frame().origin
        defaults = NSUserDefaults.standardUserDefaults()
        defaults.setObject_forKey_(f"{origin.x},{origin.y}", FRAME_KEY)

    def _restore_position(self) -> None:
        defaults = NSUserDefaults.standardUserDefaults()
        raw = defaults.stringForKey_(FRAME_KEY)
        if not raw:
            return
        try:
            xs, ys = raw.split(",", 1)
            x, y = float(xs), float(ys)
        except ValueError:
            return
        self._panel.setFrameOrigin_(NSPoint(x, y))
