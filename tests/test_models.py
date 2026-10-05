"""Unit tests for billing → UsageSnapshot mapping (no network)."""

from gbu.models import UsageSlice, snapshot_from_billing


def test_credit_usage_percent_weekly():
    payload = {
        "config": {
            "creditUsagePercent": 42.9,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "end": "2026-08-01T16:00:00Z",
            },
            "prepaidBalance": {"val": 0},
        },
        "subscriptionTiers": "SuperGrok",
    }
    snap = snapshot_from_billing(payload)
    assert snap.usage_pct == 42.9
    assert snap.usage_pct_display == 42  # floor
    assert snap.usage_label == "Weekly limit"
    assert snap.summary_lines()[0].startswith("SuperGrok: 42% · resets ")
    assert "Build 42% · Chat 0% · Imagine 0% · Voice 0%" in snap.summary_lines()[0]
    assert snap.menu_title() == "GBU · 42%"
    assert snap.subscription_tier == "SuperGrok"


def test_prepaid_credits_and_autotopup():
    payload = {
        "config": {
            "creditUsagePercent": 100.0,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY"},
            "prepaidBalance": {"val": -2500},  # accounting sign: abs → $25
        }
    }
    auto = {"rule": {"enabled": True, "topupAmount": {"val": 1000}}}
    snap = snapshot_from_billing(payload, auto_topup=auto)
    lines = "\n".join(snap.summary_lines())
    assert "Credits: $25" in lines
    assert "Auto topup: $10" in lines


def test_pay_as_you_go():
    payload = {
        "config": {
            "creditUsagePercent": 100.0,
            "onDemandCap": {"val": 5000},
            "onDemandUsed": {"val": 1234},
        }
    }
    snap = snapshot_from_billing(payload)
    assert snap.pay_as_you_go
    lines = "\n".join(snap.summary_lines())
    assert "Pay-as-you-go: $12.34 / $50" in lines


def test_legacy_limit_used():
    payload = {
        "config": {
            "monthlyLimit": {"val": 2000},
            "used": {"val": 500},
            "billingPeriodEnd": "2026-08-01T00:00:00Z",
        }
    }
    snap = snapshot_from_billing(payload)
    assert snap.usage_pct == 25.0
    assert snap.usage_pct_display == 25


def test_error_snapshot():
    snap = snapshot_from_billing({}, error="Auth rejected — open Grok Build and run /login.")
    assert snap.error
    assert snap.menu_title() == "GBU · ?"
    assert "Auth rejected" in snap.summary_lines()[0]
    assert snap.gauge_level() == "error"


def test_product_breakdown_and_bot_row():
    payload = {
        "config": {
            "creditUsagePercent": 30.0,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "end": "2026-10-11T13:40:14Z",
            },
            "productUsage": [
                {"product": "GrokBuild", "usagePercent": 30.0},
                {"product": "API", "usagePercent": 4.2},
                {"product": "GrokBot", "usagePercent": 99.0},
            ],
        }
    }
    bot = UsageSlice(
        label="Grok Bot",
        usage_pct=17.7,
        resets="Oct 9 15:38",
        plan="SuperGrok Plus",
    )
    snap = snapshot_from_billing(payload, bot=bot)
    pools = snap.pools()
    assert [sl.label for sl in pools] == ["SuperGrok Plus", "API", "Grok Bot"]
    assert pools[0].resets and pools[1].resets == pools[0].resets
    assert pools[2].resets == "Oct 9 15:38"
    assert pools[2].pct_display == 17
    assert snap.menu_title() == "GBU · 30% · Bot 17%"
    lines = "\n".join(snap.summary_lines())
    assert "SuperGrok Plus: 30% · resets " in lines
    assert "Build 30% · Chat 0% · Imagine 0% · Voice 0%" in lines
    assert "API: 4% · resets " in lines
    assert "Grok Bot: 17% · resets Oct 9 15:38" in lines
    assert "SuperGrok Plus" not in lines.split("Grok Bot:", 1)[1]
    assert snap.menu_lines()[0].startswith("SuperGrok Plus  30% · ")
    assert any(line.startswith("Grok Bot  17% · Oct 9") for line in snap.menu_lines())


def test_gauge_levels():
    ok = snapshot_from_billing({"config": {"creditUsagePercent": 26.0}})
    warn = snapshot_from_billing({"config": {"creditUsagePercent": 75.0}})
    bad = snapshot_from_billing({"config": {"creditUsagePercent": 95.0}})
    assert ok.gauge_level() == "ok"
    assert warn.gauge_level() == "warn"
    assert bad.gauge_level() == "bad"
