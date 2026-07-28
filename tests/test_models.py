"""Unit tests for billing → UsageSnapshot mapping (no network)."""

from gbu.models import snapshot_from_billing


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
    assert "Weekly limit: 42%" in snap.summary_lines()[0]
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


def test_gauge_levels():
    ok = snapshot_from_billing({"config": {"creditUsagePercent": 26.0}})
    warn = snapshot_from_billing({"config": {"creditUsagePercent": 75.0}})
    bad = snapshot_from_billing({"config": {"creditUsagePercent": 95.0}})
    assert ok.gauge_level() == "ok"
    assert warn.gauge_level() == "warn"
    assert bad.gauge_level() == "bad"
