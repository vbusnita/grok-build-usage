"""Dial and legend layout. Skipped in CI, which has no AppKit."""

import pytest

pytest.importorskip("AppKit")

from gbu.hud import (  # noqa: E402
    FUEL_DIAL_H,
    HUD_WIDTH,
    PAD_X,
    _bowl_text_width,
    _layout_dial,
    _legend_fit,
    _mono_width,
)
from gbu.models import _UNIFIED_ORDER  # noqa: E402


def _legend_used(parts):
    width = HUD_WIDTH - 2 * PAD_X
    size, swatch, gap, pad, _row_h, widths = _legend_fit(parts, width)
    used = sum(swatch + pad + tw for tw in widths) + gap * (len(parts) - 1)
    return size, used, width


def test_legend_stays_one_line_at_every_percent():
    """100% is the widest reading. Every slice at 100% still fits."""
    names = _UNIFIED_ORDER
    for pct in (0, 1, 9, 10, 99, 100, 100.9):
        parts = tuple((name, float(pct)) for name in names)
        size, used, width = _legend_used(parts)
        assert used <= width, f"{pct}% uses {used:.1f} of {width:.1f} at {size}pt"
        assert size >= 6.0


def test_legend_mixed_highs_stay_on_one_line():
    parts = (
        ("Build", 100),
        ("Chat", 0),
        ("Imagine", 100),
        ("Voice", 99),
        ("Bot", 10),
        ("Cursor", 100),
    )
    _size, used, width = _legend_used(parts)
    assert used <= width


def _assert_inside(amount, lines):
    amount_row, rows = _layout_dial(amount, lines)
    assert rows[0].text == "FUEL"
    for row in (amount_row, *rows):
        assert 2.0 <= row.y, row.text
        assert row.y + row.h <= FUEL_DIAL_H - 2.0, row.text
        mid = row.y + row.h / 2.0
        assert row.width <= _bowl_text_width(mid) + 0.5
        weight_extra = 6.0
        assert _mono_width(row.text, row.size) + weight_extra <= row.width + 0.5, row.text


def test_dial_labels_stack_inside_the_bowl():
    _assert_inside("142k", ("quiet", "Cursor"))
    _assert_inside("—", ("no live session",))
    _assert_inside("999k", ("hard", "Build", "Cursor", "Bot", "1.2M/min"))
    _assert_inside("9.8M", ("hard", "Build", "Cursor", "Bot", "last turn", "9.8M"))
    _assert_inside("9M", ("steady", "Build", "Bot", "last turn", "2.5M"))
