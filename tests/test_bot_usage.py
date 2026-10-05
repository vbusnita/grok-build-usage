"""Grok Bot status payload mapping. No network and no Keychain."""

from gbu.bot_usage import slice_from_sand_status, token_total_from_spend


def test_sand_status_percent_plan_and_reset():
    sl = slice_from_sand_status(
        {
            "usagePercent": 17.727769,
            "nextResetTimestampUtc": "2026-10-09T19:38:26.179Z",
            "grokPlanLabel": "SuperGrok Plus",
            "hasAvailableUsage": True,
        }
    )
    assert sl.label == "Grok Bot"
    assert sl.pct_display == 17
    assert sl.plan == "SuperGrok Plus"
    assert sl.detail is None
    assert sl.resets is not None and "Oct" in sl.resets
    assert sl.reset_line() == f"resets {sl.resets}"


def test_token_total_from_spend_sums_rows():
    total = token_total_from_spend(
        {
            "dailySpend": [
                {"category": "grok-bot-default", "totalTokens": "1000"},
                {"category": "grok-bot-automation", "total_tokens": 250},
                {"category": "skip", "totalTokens": "nope"},
            ]
        }
    )
    assert total == 1250


def test_sand_status_without_percent():
    sl = slice_from_sand_status({"grokPlanLabel": "SuperGrok Plus"})
    assert sl.usage_pct is None
    assert sl.value_text() == "—"
    assert sl.plan == "SuperGrok Plus"
    assert sl.detail is None
