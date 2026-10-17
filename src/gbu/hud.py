"""Floating Grok Build usage HUD — no chrome.

No card, no glass, no border: title, progress bar, and metric groups sit
directly on the desktop as translucent-free floating UI.
"""

from __future__ import annotations

import ctypes
import logging
import math
from typing import List, Optional, Sequence, Tuple

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
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
    NSMutableParagraphStyle,
    NSPanel,
    NSParagraphStyleAttributeName,
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
from gbu.models import UsageSnapshot, pct_display

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
_TICK = (1.0, 1.0, 1.0, 0.28)
# grok.com/dashboard/usage chart hues, --th-chart-1 through --th-chart-8.
# shareSeriesColor(i) resolves to slot (i % 8) + 1.
# Pinned series: Cursor 0, Bot 2, Chat 4. Build is the first unpinned series
# (index 6 → slot 7). Imagine is the next unpinned series (slot 8).
# Voice is not on the live chart; slot 9 would repeat Cursor's blue, so it
# takes slot 2, the next palette hue that stays distinct on the key.
_CHART = {
    1: (0x10 / 255, 0x84 / 255, 0xFE / 255, 1.0),  # #1084FE
    2: (0x00 / 255, 0xC9 / 255, 0x72 / 255, 1.0),  # #00C972
    3: (0xFF / 255, 0x98 / 255, 0x00 / 255, 1.0),  # #FF9800
    4: (0xFF / 255, 0x30 / 255, 0x9B / 255, 1.0),  # #FF309B
    5: (0x91 / 255, 0x59 / 255, 0xFE / 255, 1.0),  # #9159FE
    6: (0xFF / 255, 0x26 / 255, 0x3C / 255, 1.0),  # #FF263C
    7: (0x00 / 255, 0xBC / 255, 0xA6 / 255, 1.0),  # #00BCA6
    8: (0xFF / 255, 0x67 / 255, 0x00 / 255, 1.0),  # #FF6700
}
_SLICE_COLOR = {
    "Cursor": _CHART[1],
    "Bot": _CHART[3],
    "Chat": _CHART[5],
    "Build": _CHART[7],
    "Imagine": _CHART[8],
    "Voice": _CHART[2],
}
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
MASTER_BAR_H = 10.0
POOL_RESET_H = 18.0
POOL_GAP = 18.0
FUEL_DIAL_W = 260.0
FUEL_DIAL_H = 198.0
FUEL_GAP = 10.0
_DIAL_START = 210.0
_DIAL_SWEEP = 240.0
FRAME_KEY = "gbu.hudLastOrigin"
VISIBLE_KEY = "gbu.hudVisible"
_TOOLS_SYMBOL = "wrench.and.screwdriver"


def _ns_color(rgba: Tuple[float, float, float, float]) -> NSColor:
    r, g, b, a = rgba
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


_CG_CACHE: dict = {}
_CGCOLOR_READY = False


def _register_cgcolor() -> None:
    """Teach PyObjC that CGColor is a CoreFoundation type.

    Without this, ``NSColor.CGColor()`` is a raw unowned pointer. Caching that
    pointer and passing it to ``CALayer.setBackgroundColor:`` on a later timer
    CFRetains a freed color. The process dies with SIGTRAP and LaunchAgent
    brings the overlay back. ``CFRetain`` on the raw pointer does not keep it.
    """
    global _CGCOLOR_READY
    if _CGCOLOR_READY:
        return
    lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    lib.CGColorGetTypeID.restype = ctypes.c_ulong
    objc.registerCFSignature("CGColor", b"^{CGColor=}", lib.CGColorGetTypeID())
    _CGCOLOR_READY = True


_register_cgcolor()


def _cg_color(rgba: Tuple[float, float, float, float]):
    key = tuple(rgba)
    cg = _CG_CACHE.get(key)
    if cg is not None:
        return cg
    cg = _ns_color(rgba).CGColor()
    _CG_CACHE[key] = cg
    return cg


def saved_hud_visible() -> Optional[bool]:
    """User's last Hide/Show choice, or None if they have not chosen yet."""
    defaults = NSUserDefaults.standardUserDefaults()
    if defaults.objectForKey_(VISIBLE_KEY) is None:
        return None
    return bool(defaults.boolForKey_(VISIBLE_KEY))


def set_hud_visible(visible: bool) -> None:
    NSUserDefaults.standardUserDefaults().setBool_forKey_(bool(visible), VISIBLE_KEY)


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
    elif align == "center":
        lab.setAlignment_(1)
    _LABEL_STYLE[id(lab)] = {"font": font, "color": color, "kern": kern, "align": align}
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
    paragraph = NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_({"center": 1, "right": 2}.get(style.get("align", "left"), 0))
    attrs = NSDictionary.dictionaryWithObjects_forKeys_(
        [font, _ns_color(rgba), float(k), _text_shadow(), paragraph],
        [
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSKernAttributeName,
            NSShadowAttributeName,
            NSParagraphStyleAttributeName,
        ],
    )
    lab.setAttributedStringValue_(
        NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    )


def _mono_width(text: str, size: float, weight=NSFontWeightMedium) -> float:
    font = NSFont.monospacedSystemFontOfSize_weight_(size, weight)
    astr = NSAttributedString.alloc().initWithString_attributes_(
        text,
        NSDictionary.dictionaryWithObjects_forKeys_([font], [NSFontAttributeName]),
    )
    return float(astr.size().width)


def _legend_fit(
    parts: Sequence[Tuple[str, float]],
    width: float,
) -> Tuple[float, float, float, float, float, List[float]]:
    """Largest type that keeps every ``Name N%`` on one line.

    ``pct_display`` clamps at 100, so the widest line is every slice at 100%.
    The search keeps shrinking until that line fits ``width``.

    Returns font size, swatch, gap, swatch-to-text pad, row height, and the
    measured width of each label.
    """
    labels = [f"{name} {pct_display(pct)}%" for name, pct in parts]
    if not labels:
        return 11.0, 6.0, 8.0, 3.0, 16.0, []
    slots = len(labels) - 1
    # 13pt down to 6pt. Shrink the gap before giving up a half point.
    for step in range(26, 11, -1):
        size = step / 2.0
        swatch = 6.0 if size >= 10.0 else (5.0 if size >= 8.0 else 4.0)
        pad = 3.0 if size >= 9.0 else 2.0
        # Extra points so the text field's inset does not clip the percent.
        slack = 8.0 if size >= 8.0 else 6.0
        widths = [_mono_width(text, size) + slack for text in labels]
        body = sum(swatch + pad + tw for tw in widths)
        for gap in (8.0, 6.0, 4.0, 2.0):
            if body + gap * slots <= width:
                return size, swatch, gap, pad, size + 5.0, widths
    size = 6.0
    swatch, pad, gap, slack = 4.0, 2.0, 2.0, 4.0
    widths = [_mono_width(text, size) + slack for text in labels]
    return size, swatch, gap, pad, size + 4.0, widths


def _pool_block_height(*, master: bool, legend_h: float, extra_h: float) -> float:
    bar_h = MASTER_BAR_H if master else POOL_BAR_H
    below = 4.0 if legend_h or extra_h else 0.0
    return POOL_NAME_H + 8 + bar_h + 6 + POOL_RESET_H + below + legend_h + extra_h + POOL_GAP


def _fuel_block_height() -> float:
    return FUEL_DIAL_H + FUEL_GAP


def _dial_frame(
    width: float = FUEL_DIAL_W,
    height: float = FUEL_DIAL_H,
) -> Tuple[float, float, float, float]:
    """Center, outer radius, and the radius text must stay inside."""
    radius = min(width / 2.0 - 8.0, height - 12.0)
    cx = width / 2.0
    cy = height - 6.0 - radius
    inner = max(20.0, radius - 18.0)
    return cx, cy, radius, inner


def _bowl_text_width(y_mid: float) -> float:
    """How wide a centered line can be at ``y_mid`` without crossing the ticks."""
    _cx, cy, _radius, inner = _dial_frame()
    dy = y_mid - cy
    limit = inner - 2.0
    if abs(dy) >= limit:
        return 40.0
    chord = 2.0 * math.sqrt(limit * limit - dy * dy)
    return max(40.0, chord - 10.0)


class _DialRow:
    def __init__(self, text: str, size: float, y: float, h: float, width: float, kern: float):
        self.text = text
        self.size = size
        self.y = y
        self.h = h
        self.width = width
        self.kern = kern


def _split_to_width(text: str, size: float, max_width: float) -> List[str]:
    """Break a phrase into lines that each fit ``max_width``."""
    if _mono_width(text, size) + 6.0 <= max_width:
        return [text]
    words = text.split()
    if len(words) <= 1:
        return [text]
    lines: List[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if _mono_width(trial, size) + 6.0 <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _layout_dial(amount: str, lines: Sequence[str]) -> Tuple[_DialRow, List[_DialRow]]:
    """Stack the readout and its labels inside the open arc.

    The number sits high in the bowl. ``FUEL`` and each pace phrase sit on
    their own line under it, narrowed to the chord at that height.
    """
    _cx, cy, radius, inner = _dial_frame()
    # Stay above the open ends of the arc (210° and -30°, sin = -0.5).
    top = cy + inner - 8.0
    bottom = max(8.0, cy - 0.5 * radius + 8.0)
    phrases = ["FUEL", *lines]

    def fits(text: str, size: float, y: float, h: float, kern: float) -> bool:
        mid = y + h / 2.0
        if mid < bottom or y < 2.0 or y + h > FUEL_DIAL_H - 2.0:
            return False
        need = _mono_width(text, size, NSFontWeightSemibold if kern < 0 else NSFontWeightMedium)
        need += max(0, len(text) - 1) * max(0.0, kern) + 6.0
        return need <= _bowl_text_width(mid)

    for amount_size in (34, 32, 30, 28, 26, 24, 22, 20):
        for label_size in (12.0, 11.0, 10.0, 9.0, 8.0):
            amount_h = amount_size + 6.0
            label_h = label_size + 4.0
            packed: List[Tuple[str, float]] = []
            for phrase in phrases:
                kern = 1.5 if phrase == "FUEL" else 0.0
                parts = _split_to_width(phrase, label_size, _bowl_text_width(cy))
                packed.extend((part, kern) for part in parts)
            block = amount_h + 2.0 + label_h * len(packed)
            y_amount = top - amount_h
            if y_amount - block + amount_h < bottom:
                continue
            if not fits(amount, float(amount_size), y_amount, amount_h, -0.6):
                continue
            y = y_amount - 2.0
            rows: List[_DialRow] = []
            ok = True
            for text, kern in packed:
                y -= label_h
                if not fits(text, label_size, y, label_h, kern):
                    ok = False
                    break
                mid = y + label_h / 2.0
                rows.append(
                    _DialRow(text, label_size, y, label_h, _bowl_text_width(mid), kern)
                )
            if ok:
                mid = y_amount + amount_h / 2.0
                return (
                    _DialRow(
                        amount,
                        float(amount_size),
                        y_amount,
                        amount_h,
                        _bowl_text_width(mid),
                        -0.6,
                    ),
                    rows,
                )
    # Smallest sizes still have to land inside the bowl.
    amount_h = 26.0
    label_h = 12.0
    y_amount = top - amount_h
    rows = []
    y = y_amount - 2.0
    for phrase in phrases:
        y -= label_h
        mid = y + label_h / 2.0
        rows.append(_DialRow(phrase, 8.0, y, label_h, _bowl_text_width(mid), 0.0))
    mid = y_amount + amount_h / 2.0
    return _DialRow(amount, 20.0, y_amount, amount_h, _bowl_text_width(mid), -0.6), rows


class FuelDialView(NSView):
    """Open arc and tick ring. The digital readout sits on top as text fields."""

    def initWithFrame_(self, frame):  # noqa: N802
        self = objc.super(FuelDialView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.fillFraction = 0.0
        self.strokeColor = _ns_color(_TELEM_VALUE)
        return self

    def isOpaque(self):  # noqa: N802
        return False

    def drawRect_(self, _dirty):  # noqa: N802
        bounds = self.bounds()
        cx, cy, radius, _inner = _dial_frame(bounds.size.width, bounds.size.height)
        if radius < 20.0:
            return
        center = NSPoint(cx, cy)
        start = _DIAL_START
        sweep = _DIAL_SWEEP
        fill = max(0.0, min(1.0, float(self.fillFraction)))

        track = NSBezierPath.bezierPath()
        track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            center, radius, start, start - sweep, True
        )
        track.setLineWidth_(3.0)
        track.setLineCapStyle_(1)
        _ns_color(_TRACK).setStroke()
        track.stroke()

        ticks = 24
        for index in range(ticks + 1):
            t = index / ticks
            ang = math.radians(start - t * sweep)
            major = index % 6 == 0
            outer_r = radius - 7.0
            inner_r = outer_r - (9.0 if major else 4.5)
            tick = NSBezierPath.bezierPath()
            tick.moveToPoint_(
                NSPoint(cx + inner_r * math.cos(ang), cy + inner_r * math.sin(ang))
            )
            tick.lineToPoint_(
                NSPoint(cx + outer_r * math.cos(ang), cy + outer_r * math.sin(ang))
            )
            tick.setLineWidth_(1.6 if major else 1.0)
            tick.setLineCapStyle_(1)
            if fill > 0.0 and t <= fill + 1e-6:
                self.strokeColor.setStroke()
            else:
                _ns_color(_TICK).setStroke()
            tick.stroke()

        if fill <= 0.004:
            return
        value = NSBezierPath.bezierPath()
        value.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            center, radius, start, start - fill * sweep, True
        )
        value.setLineWidth_(3.5)
        value.setLineCapStyle_(1)
        self.strokeColor.setStroke()
        value.stroke()
        tip = math.radians(start - fill * sweep)
        tx = cx + radius * math.cos(tip)
        ty = cy + radius * math.sin(tip)
        dot_r = 3.5
        dot = NSBezierPath.bezierPathWithOvalInRect_(
            NSRect(NSPoint(tx - dot_r, ty - dot_r), NSSize(dot_r * 2, dot_r * 2))
        )
        self.strokeColor.setFill()
        dot.fill()


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
        if self.isVisible():
            self._redraw()

    def update_fuel(self, reading: Optional[FuelReading]) -> None:
        self._fuel = reading if reading is not None else FuelReading.none()
        if self.isVisible():
            self._redraw()

    def _redraw(self) -> None:
        if self._snapshot is None:
            self._apply_loading()
            return
        self._apply_snapshot(self._snapshot)

    def show(self) -> None:
        self._panel.orderFront_(None)
        self._redraw()

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
        """Fuel block whose top sits at top_edge. The number is tokens; the arc is pace."""
        reading = self._fuel
        dial_x = (HUD_WIDTH - FUEL_DIAL_W) / 2.0
        dial_y = top_edge - FUEL_DIAL_H
        dial = FuelDialView.alloc().initWithFrame_(
            NSRect(NSPoint(dial_x, dial_y), NSSize(FUEL_DIAL_W, FUEL_DIAL_H))
        )
        amount_color = self._pace_color(reading.level) if reading.sources else _TEXT_MUTED
        dial.fillFraction = reading.fill()
        dial.strokeColor = _ns_color(amount_color)
        self._track_breakdown(dial)

        amount, rows = _layout_dial(reading.amount_text(), reading.detail_lines())
        cx = _dial_frame()[0]

        def _place(row: _DialRow, color, weight) -> None:
            field = _make_text_field(
                dial_x + cx - row.width / 2.0,
                dial_y + row.y,
                row.width,
                row.h,
                size=row.size,
                weight=weight,
                color=color,
                align="center",
                mono=True,
                kern=row.kern,
            )
            _set_text(field, row.text, color=color, kern=row.kern)
            self._track_breakdown(field)

        _place(amount, amount_color, NSFontWeightSemibold)
        for row in rows:
            color = _TELEM_LABEL if row.text == "FUEL" else _TELEM_VALUE
            weight = NSFontWeightMedium if row.text == "FUEL" else NSFontWeightRegular
            _place(row, color, weight)

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

    def _add_stacked_bar(
        self,
        x: float,
        y: float,
        width: float,
        parts: Sequence[Tuple[str, float]],
        bar_h: float,
    ) -> None:
        """One plan bar. Each product is a colored share of the week."""
        track = NSView.alloc().initWithFrame_(
            NSRect(NSPoint(x, y), NSSize(width, bar_h))
        )
        track.setWantsLayer_(True)
        track.layer().setCornerRadius_(bar_h / 2)
        track.layer().setMasksToBounds_(True)
        track.layer().setBackgroundColor_(_cg_color(_TRACK))
        self._track_breakdown(track)
        widths: List[Tuple[str, float]] = []
        for name, pct in parts:
            if pct <= 0:
                continue
            widths.append((name, max(2.0, pct / 100.0 * width)))
        total = sum(seg_w for _name, seg_w in widths)
        if total > width and total > 0:
            scale = width / total
            widths = [(name, seg_w * scale) for name, seg_w in widths]
        cursor = 0.0
        for name, seg_w in widths:
            fill = NSView.alloc().initWithFrame_(
                NSRect(NSPoint(cursor, 0), NSSize(seg_w, bar_h))
            )
            fill.setWantsLayer_(True)
            color = _SLICE_COLOR.get(name, _TELEM_VALUE)
            fill.layer().setBackgroundColor_(_cg_color(color))
            track.addSubview_(fill)
            cursor += seg_w

    def _draw_legend(
        self,
        parts: Sequence[Tuple[str, float]],
        x: float,
        bottom: float,
        width: float,
    ) -> None:
        """One-line color key. ``bottom`` is the legend's lower edge."""
        if not parts:
            return
        size, swatch, gap, pad, row_h, widths = _legend_fit(parts, width)
        labels = [f"{name} {pct_display(pct)}%" for name, pct in parts]
        used = sum(swatch + pad + tw for tw in widths) + gap * (len(parts) - 1)
        cursor = x + max(0.0, (width - used) / 2.0)
        for (name, _pct), text, label_w in zip(parts, labels, widths):
            color = _SLICE_COLOR.get(name, _TELEM_VALUE)
            swatch_y = bottom + (row_h - swatch) / 2.0
            dot = NSView.alloc().initWithFrame_(
                NSRect(NSPoint(cursor, swatch_y), NSSize(swatch, swatch))
            )
            dot.setWantsLayer_(True)
            dot.layer().setCornerRadius_(swatch / 2.0)
            dot.layer().setBackgroundColor_(_cg_color(color))
            self._track_breakdown(dot)
            cursor += swatch + pad
            label = _make_text_field(
                cursor,
                bottom,
                label_w,
                row_h,
                size=size,
                weight=NSFontWeightMedium,
                color=color,
                mono=True,
            )
            _set_text(label, text, color=color)
            self._track_breakdown(label)
            cursor += label_w + gap

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
        plan = snap.plan_name()
        parts = () if snap.error else tuple(snap.breakdown_parts())
        extra = None if snap.error else snap.account_footnote()
        bar_w = HUD_WIDTH - 2 * PAD_X
        legend_h = _legend_fit(parts, bar_w)[4] if parts else 0.0
        extra_h = 16.0 if extra else 0.0
        heights = [
            _pool_block_height(
                master=sl.label == plan and not snap.error,
                legend_h=legend_h if sl.label == plan else 0.0,
                extra_h=extra_h if sl.label == plan else 0.0,
            )
            for sl in pools
        ]
        fuel_h = _fuel_block_height()
        height = PAD_Y + fuel_h + sum(heights) + PAD_Y
        self._fit_pools(height)
        self._draw_fuel(height - PAD_Y)
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

            master = sl.label == plan and not snap.error
            bar_h = MASTER_BAR_H if master else POOL_BAR_H
            bar_y = name_y - 8 - bar_h
            if master and parts:
                self._add_stacked_bar(PAD_X, bar_y, bar_w, parts, bar_h)
            else:
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

            if master and parts:
                self._draw_legend(parts, PAD_X, reset_y - 4 - legend_h, bar_w)
            if master and extra:
                note_y = reset_y - 4 - legend_h - extra_h
                note = _make_text_field(
                    PAD_X,
                    note_y,
                    bar_w,
                    extra_h,
                    size=LABEL_SIZE,
                    weight=NSFontWeightRegular,
                    color=_TEXT_MUTED,
                    mono=True,
                )
                _set_text(note, extra, color=_TEXT_MUTED)
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
