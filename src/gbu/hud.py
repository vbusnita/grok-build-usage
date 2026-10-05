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
    NSFloatingWindowLevel,
    NSShadow,
    NSShadowAttributeName,
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

from gbu.fuel import FuelReading
from gbu.models import UsageSlice, UsageSnapshot

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
POOL_NAME_H = 24.0
POOL_BAR_H = 6.0
POOL_RESET_H = 18.0
POOL_GAP = 18.0
FUEL_NAME_H = 24.0
FUEL_BAR_H = 8.0
FUEL_DETAIL_H = 18.0
FUEL_GAP = 20.0
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


def _pool_block_height(footnote: bool) -> float:
    height = POOL_NAME_H + 8 + POOL_BAR_H + 6 + POOL_RESET_H
    if footnote:
        height += 16
    return height + POOL_GAP


def _fuel_block_height() -> float:
    return FUEL_NAME_H + 8 + FUEL_BAR_H + 6 + FUEL_DETAIL_H + FUEL_GAP


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
        self._breakdown_views: List[NSView] = []
        self._fuel = FuelReading.none()
        self._build_panel()
        self._restore_position()
        self._apply_loading()
        return self

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_snapshot(self, snapshot: Optional[UsageSnapshot]) -> None:
        self._snapshot = snapshot
        self._redraw()

    def update_fuel(self, reading: Optional[FuelReading]) -> None:
        self._fuel = reading if reading is not None else FuelReading.none()
        self._redraw()

    def _redraw(self) -> None:
        if self._snapshot is None:
            self._apply_loading()
            return
        self._apply_snapshot(self._snapshot)

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
        height = self._panel_height(3, 0.0)
        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSRect(NSPoint(40, 80), NSSize(HUD_WIDTH, height)),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        # Must NOT use NSStatusWindowLevel: a full-width HUD at status-bar
        # level competes with NSStatusItem layout and leaves the menu-bar
        # button at height 0 forever (seen on macOS 15+). Floating is enough
        # for an always-on-top glanceable overlay.
        self._panel.setLevel_(NSFloatingWindowLevel)
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

        self._breakdown_host = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, 1))
        )
        content.addSubview_(self._breakdown_host)
        for view in (
            self._icon_view,
            self._brand,
            self._live,
            self._track,
            self._gauge_pct,
            self._gauge_label,
            self._metrics_host,
        ):
            view.setHidden_(True)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _panel_height(self, metric_n: int, breakdown_h: float) -> float:
        # Keeps an 8pt gap between the gauge caption and the breakdown block.
        metrics_extra = (METRIC_BLOCK_H + 8) if metric_n else 0
        return 2 * PAD_Y + metrics_extra + breakdown_h + ICON_PT + 58

    def _place_lower(self, breakdown_h: float, metric_n: int) -> None:
        if metric_n:
            self._metrics_host.setFrame_(
                NSRect(NSPoint(0, PAD_Y), NSSize(HUD_WIDTH, METRIC_BLOCK_H))
            )
        else:
            self._metrics_host.setFrame_(NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, 1)))
        metrics_extra = (METRIC_BLOCK_H + 8) if metric_n else 0
        self._breakdown_host.setFrame_(
            NSRect(
                NSPoint(0, PAD_Y + metrics_extra),
                NSSize(HUD_WIDTH, max(breakdown_h, 1.0)),
            )
        )

    def _resize_to(self, height: float, breakdown_h: float = 0.0, metric_n: int = 0) -> None:
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
        self._place_lower(breakdown_h, metric_n)

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

    def _render_metrics(self, rows: Sequence[Tuple[str, str]]) -> None:
        self._clear_metrics()
        if not rows:
            return

        rows = list(rows)[:4]
        n = len(rows)

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

    def _level_color(self, level: str):
        if level == "bad" or level == "error":
            return _RED
        if level == "warn":
            return _YELLOW
        return _GREEN

    def _pace_color(self, level: str):
        if level == "hard":
            return _RED
        if level == "steady":
            return _YELLOW
        if level == "easy":
            return _GREEN
        return _TELEM_VALUE

    def _draw_fuel(self, top_edge: float) -> None:
        """Fuel block whose top sits at top_edge. Amount is tokens; bar is pace."""
        reading = self._fuel
        name_y = top_edge - FUEL_NAME_H
        label = _make_text_field(
            PAD_X,
            name_y,
            HUD_WIDTH - 120,
            FUEL_NAME_H,
            size=18,
            weight=NSFontWeightSemibold,
            color=_TEXT,
            mono=False,
        )
        _set_text(label, "Fuel")
        self._track_breakdown(label)

        amount_color = self._pace_color(reading.level) if reading.sources else _TEXT_MUTED
        amount = _make_text_field(
            HUD_WIDTH - PAD_X - 100,
            name_y,
            100,
            FUEL_NAME_H,
            size=20,
            weight=NSFontWeightSemibold,
            color=amount_color,
            align="right",
            mono=True,
        )
        _set_text(amount, reading.amount_text(), color=amount_color)
        self._track_breakdown(amount)

        bar_y = name_y - 8 - FUEL_BAR_H
        self._add_pool_bar(
            PAD_X,
            bar_y,
            HUD_WIDTH - 2 * PAD_X,
            reading.fill() * 100.0,
            amount_color,
            bar_h=FUEL_BAR_H,
        )

        detail_y = bar_y - 6 - FUEL_DETAIL_H
        detail = _make_text_field(
            PAD_X,
            detail_y,
            HUD_WIDTH - 2 * PAD_X,
            FUEL_DETAIL_H,
            size=15,
            weight=NSFontWeightRegular,
            color=_TELEM_VALUE,
            mono=True,
        )
        _set_text(detail, reading.detail_text(), color=_TELEM_VALUE)
        self._track_breakdown(detail)

    def _clear_breakdown(self) -> None:
        for view in self._breakdown_views:
            view.removeFromSuperview()
        self._breakdown_views = []

    def _track_breakdown(self, view: NSView) -> None:
        self._breakdown_host.addSubview_(view)
        self._breakdown_views.append(view)

    def _fit_pools(self, height: float) -> None:
        frame = self._panel.frame()
        new_y = frame.origin.y + (frame.size.height - height)
        self._panel.setFrame_display_(
            NSRect(NSPoint(frame.origin.x, new_y), NSSize(HUD_WIDTH, height)),
            True,
        )
        self._content.setFrame_(NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, height)))
        self._breakdown_host.setFrame_(
            NSRect(NSPoint(0, 0), NSSize(HUD_WIDTH, height))
        )

    def _add_pool_bar(
        self,
        x: float,
        y: float,
        width: float,
        pct: Optional[float],
        color,
        bar_h: float = POOL_BAR_H,
    ) -> None:
        track = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(x, y), NSSize(width, bar_h))
        )
        track.setWantsLayer_(True)
        track.layer().setCornerRadius_(bar_h / 2)
        track.layer().setMasksToBounds_(True)
        track.layer().setBackgroundColor_(_cg_color(_TRACK))
        self._track_breakdown(track)
        frac = 0.0 if pct is None else max(0.0, min(1.0, pct / 100.0))
        fill_w = frac * width
        if pct and fill_w < 4:
            fill_w = 4
        fill = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), NSSize(fill_w, bar_h))
        )
        fill.setWantsLayer_(True)
        fill.layer().setCornerRadius_(bar_h / 2)
        fill.layer().setBackgroundColor_(_cg_color(color if pct is not None else _TRACK))
        track.addSubview_(fill)

    def _render_pools(self, snap: UsageSnapshot) -> None:
        """One block per allowance: name, percent, bar, then that block's reset."""
        self._clear_breakdown()
        pools = snap.pools()
        if not pools:
            fuel_h = _fuel_block_height()
            height = PAD_Y + fuel_h + PAD_Y
            self._fit_pools(height)
            self._draw_fuel(height - PAD_Y)
            return
        footnote = None if snap.error else snap.plan_note()
        plan = snap.plan_name()
        heights = [
            _pool_block_height(bool(footnote) and sl.label == plan)
            for sl in pools
        ]
        fuel_h = _fuel_block_height()
        height = PAD_Y + fuel_h + sum(heights) + PAD_Y
        self._fit_pools(height)
        self._draw_fuel(height - PAD_Y)
        bar_w = HUD_WIDTH - 2 * PAD_X
        top = height - PAD_Y - fuel_h
        for sl, block_h in zip(pools, heights):
            top -= block_h
            content_top = top + block_h - POOL_GAP
            name_y = content_top - POOL_NAME_H
            label = _make_text_field(
                PAD_X,
                name_y,
                HUD_WIDTH - 110,
                POOL_NAME_H,
                size=18,
                weight=NSFontWeightSemibold,
                color=_TEXT,
                mono=False,
            )
            _set_text(label, sl.label)
            self._track_breakdown(label)

            pct_color = self._level_color(sl.level()) if sl.usage_pct is not None else _TEXT_MUTED
            pct = _make_text_field(
                HUD_WIDTH - PAD_X - 84,
                name_y,
                84,
                POOL_NAME_H,
                size=20,
                weight=NSFontWeightSemibold,
                color=pct_color,
                align="right",
                mono=True,
            )
            _set_text(pct, sl.value_text(), color=pct_color)
            self._track_breakdown(pct)

            bar_y = name_y - 8 - POOL_BAR_H
            self._add_pool_bar(PAD_X, bar_y, bar_w, sl.usage_pct, pct_color)

            reset_y = bar_y - 6 - POOL_RESET_H
            reset = _make_text_field(
                PAD_X,
                reset_y,
                bar_w,
                POOL_RESET_H,
                size=15,
                weight=NSFontWeightRegular,
                color=_TELEM_VALUE,
                mono=True,
            )
            reset_text = sl.reset_line() or "reset time unavailable"
            _set_text(reset, reset_text, color=_TELEM_VALUE)
            self._track_breakdown(reset)

            if footnote and sl.label == plan:
                note = _make_text_field(
                    PAD_X,
                    reset_y - 16,
                    bar_w,
                    16,
                    size=LABEL_SIZE,
                    weight=NSFontWeightRegular,
                    color=_TEXT_MUTED,
                    mono=True,
                )
                _set_text(note, footnote, color=_TEXT_MUTED)
                self._track_breakdown(note)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _apply_loading(self) -> None:
        self._clear_breakdown()
        fuel_h = _fuel_block_height()
        height = PAD_Y + fuel_h + POOL_NAME_H + PAD_Y
        self._fit_pools(height)
        self._draw_fuel(height - PAD_Y)
        loading = _make_text_field(
            PAD_X,
            PAD_Y,
            HUD_WIDTH - 2 * PAD_X,
            POOL_NAME_H,
            size=18,
            weight=NSFontWeightSemibold,
            color=_TEXT_MUTED,
            mono=False,
        )
        _set_text(loading, "Loading usage", color=_TEXT_MUTED)
        self._track_breakdown(loading)

    def _apply_snapshot(self, snap: UsageSnapshot) -> None:
        self._render_pools(snap)

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
