"""Usage snapshot models aligned with Grok Build's billing / credit_bar types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


def _cent_val(obj: Any) -> Optional[int]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        val = obj.get("val", 0)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
    try:
        return int(obj)
    except (TypeError, ValueError):
        return None


def _fmt_dollars(cents: int) -> str:
    dollars = abs(cents) / 100.0
    if dollars == int(dollars):
        return f"${int(dollars)}"
    return f"${dollars:.2f}"


def _parse_period_end(iso: Optional[str]) -> Optional[str]:
    """Compact local wall time for telem pills — always includes HH:MM.

    Format: ``Aug 2 09:40`` (no comma) so it fits node chips without
    clipping the clock off after the day number.
    """
    if not iso:
        return None
    try:
        raw = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        local = dt.astimezone()
        # %-d is platform-dependent; fall back if needed.
        try:
            return local.strftime("%b %-d %H:%M")
        except ValueError:
            return local.strftime("%b %d %H:%M").replace(" 0", " ", 1)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class UsageSnapshot:
    """Normalized glanceable usage state for the HUD + menu bar."""

    usage_pct: float
    effective_usage_pct: float
    usage_label: str
    period_end_display: Optional[str]
    pay_as_you_go: bool
    on_demand_cap_cents: Optional[int]
    on_demand_used_cents: Optional[int]
    prepaid_balance_cents: Optional[int]
    period_type: Optional[str]
    subscription_tier: Optional[str]
    auto_topup_enabled: Optional[bool]
    auto_topup_amount_cents: Optional[int]
    error: Optional[str] = None

    @property
    def usage_pct_display(self) -> int:
        """Floor to match Build SpendingLimiter truncation."""
        return max(0, min(100, int(self.usage_pct // 1)))

    def menu_title(self) -> str:
        if self.error:
            return "GBU · ?"
        return f"GBU · {self.usage_pct_display}%"

    def gauge_level(self) -> str:
        """ok | warn | bad | error — mirrors Lyra bar-gauge / status-hud bands."""
        if self.error:
            return "error"
        pct = self.usage_pct
        if pct >= 90.0:
            return "bad"
        if pct >= 70.0:
            return "warn"
        return "ok"

    def prepaid_display(self) -> Optional[str]:
        prepaid = self.prepaid_balance_cents
        if prepaid is None or abs(prepaid) <= 0:
            return None
        return _fmt_dollars(prepaid)

    def metric_rows(self) -> list[tuple[str, str]]:
        """Label/value pairs for instrument chips under the gauge."""
        if self.error:
            return [
                ("STATUS", "AUTH"),
                ("HINT", "grok login"),
            ]

        rows: list[tuple[str, str]] = []
        if self.period_end_display:
            rows.append(("RESET", self.period_end_display))

        prepaid = self.prepaid_display()
        if prepaid:
            rows.append(("CREDITS", prepaid))
            if self.auto_topup_enabled is True and self.auto_topup_amount_cents is not None:
                rows.append(("AUTO TOPUP", _fmt_dollars(self.auto_topup_amount_cents)))
            elif self.auto_topup_enabled is False:
                rows.append(("AUTO TOPUP", "off"))

        if self.pay_as_you_go:
            used = abs(self.on_demand_used_cents or 0)
            cap = abs(self.on_demand_cap_cents or 0)
            rows.append(("PAYG", f"{_fmt_dollars(used)} / {_fmt_dollars(cap)}"))

        if self.subscription_tier:
            rows.append(("TIER", self.subscription_tier))

        return rows

    def summary_lines(self) -> list[str]:
        """Plain-text lines (CLI `--once` / debug)."""
        if self.error:
            return [self.error, "Run `grok login` if auth expired."]

        lines = [f"{self.usage_label}: {self.usage_pct_display}%"]
        for label, value in self.metric_rows():
            if label == "STATUS":
                continue
            pretty = {
                "RESET": "Next reset",
                "CREDITS": "Credits",
                "AUTO TOPUP": "Auto topup",
                "PAYG": "Pay-as-you-go",
                "TIER": "Tier",
                "HINT": "Hint",
            }.get(label, label.title())
            lines.append(f"{pretty}: {value}")
        return lines


def snapshot_from_billing(
    payload: dict[str, Any],
    *,
    auto_topup: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> UsageSnapshot:
    """Map backend billing JSON → UsageSnapshot."""
    if error:
        return UsageSnapshot(
            usage_pct=0.0,
            effective_usage_pct=0.0,
            usage_label="Usage",
            period_end_display=None,
            pay_as_you_go=False,
            on_demand_cap_cents=None,
            on_demand_used_cents=None,
            prepaid_balance_cents=None,
            period_type=None,
            subscription_tier=None,
            auto_topup_enabled=None,
            auto_topup_amount_cents=None,
            error=error,
        )

    # Backend may nest under "config" (BillingConfigResponse) or be flat.
    config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not isinstance(config, dict):
        config = {}

    credit_pct = config.get("creditUsagePercent")
    if credit_pct is None:
        credit_pct = config.get("credit_usage_percent")

    limit = _cent_val(config.get("monthlyLimit") or config.get("monthly_limit")) or 0
    used = _cent_val(config.get("used")) or 0

    if credit_pct is not None:
        try:
            usage_pct = max(0.0, min(100.0, float(credit_pct)))
            has_credit_pct = True
        except (TypeError, ValueError):
            usage_pct = 0.0
            has_credit_pct = False
    elif limit > 0:
        usage_pct = min(100.0, used / limit * 100.0)
        has_credit_pct = False
    else:
        usage_pct = 0.0
        has_credit_pct = False

    current_period = config.get("currentPeriod") or config.get("current_period") or {}
    if not isinstance(current_period, dict):
        current_period = {}
    period_type = current_period.get("type") or current_period.get("period_type")
    period_end = (
        current_period.get("end")
        or config.get("billingPeriodEnd")
        or config.get("billing_period_end")
    )

    on_demand_cap = _cent_val(config.get("onDemandCap") or config.get("on_demand_cap")) or 0
    on_demand_used = _cent_val(config.get("onDemandUsed") or config.get("on_demand_used"))
    if on_demand_used is None:
        on_demand_used = max(0, used - limit) if on_demand_cap > 0 else 0

    pay_as_you_go = on_demand_cap > 0
    if pay_as_you_go and usage_pct >= 100.0:
        effective = min(100.0, on_demand_used / on_demand_cap * 100.0) if on_demand_cap else 0.0
    elif pay_as_you_go and not has_credit_pct:
        total = limit + on_demand_cap
        effective = min(100.0, used / total * 100.0) if total else 0.0
    else:
        effective = usage_pct

    prepaid = _cent_val(config.get("prepaidBalance") or config.get("prepaid_balance"))

    if period_type and "WEEKLY" in str(period_type).upper():
        label = "Weekly limit"
    elif period_type and "MONTHLY" in str(period_type).upper():
        label = "Monthly limit"
    else:
        label = "Usage"

    auto_enabled: Optional[bool] = None
    auto_amount: Optional[int] = None
    if auto_topup is not None:
        rule = auto_topup.get("rule") if isinstance(auto_topup.get("rule"), dict) else auto_topup
        if isinstance(rule, dict):
            auto_enabled = bool(rule.get("enabled", False))
            topup = rule.get("topupAmount") or rule.get("topup_amount")
            auto_amount = _cent_val(topup)

    # Build enriches tier from remote settings; wire names vary by path.
    tier = (
        payload.get("subscription_tier")
        or payload.get("subscriptionTier")
        or payload.get("subscriptionTiers")
    )

    return UsageSnapshot(
        usage_pct=usage_pct,
        effective_usage_pct=effective,
        usage_label=label,
        period_end_display=_parse_period_end(period_end if isinstance(period_end, str) else None),
        pay_as_you_go=pay_as_you_go,
        on_demand_cap_cents=on_demand_cap if on_demand_cap > 0 else None,
        on_demand_used_cents=on_demand_used if pay_as_you_go else None,
        prepaid_balance_cents=prepaid,
        period_type=str(period_type) if period_type else None,
        subscription_tier=str(tier) if tier else None,
        auto_topup_enabled=auto_enabled,
        auto_topup_amount_cents=auto_amount,
        error=None,
    )
