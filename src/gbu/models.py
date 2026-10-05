"""Usage snapshot models aligned with Grok Build's billing / credit_bar types."""

from __future__ import annotations

import re
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


_PRODUCT_LABELS = {
    "grokbuild": "Grok Build",
    "grokbot": "Grok Bot",
    "sand": "Grok Bot",
    "grok": "Grok",
    "api": "API",
}


# Products that draw the shared weekly pool. Shown in the plan note, not as rows.
_SHARED_SLICE = {
    "Grok Build": "Build",
    "Build": "Build",
    "Chat": "Chat",
    "Grok Chat": "Chat",
    "Imagine": "Imagine",
    "Grok Imagine": "Imagine",
    "Voice": "Voice",
    "Grok Voice": "Voice",
}
_SHARED_ROW_LABELS = frozenset(_SHARED_SLICE)


def product_label(raw: str) -> str:
    """Human label for a billing productUsage name."""
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    if key in _PRODUCT_LABELS:
        return _PRODUCT_LABELS[key]
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).replace("_", " ").strip()
    return spaced or raw


def pct_display(pct: float) -> int:
    """Floor to match Build SpendingLimiter truncation."""
    return max(0, min(100, int(pct // 1)))


@dataclass(frozen=True)
class UsageSlice:
    """One allowance: its own percent and its own reset clock."""

    label: str
    usage_pct: Optional[float]
    resets: Optional[str] = None
    detail: Optional[str] = None
    plan: Optional[str] = None

    @property
    def pct_display(self) -> Optional[int]:
        if self.usage_pct is None:
            return None
        return pct_display(self.usage_pct)

    def level(self) -> str:
        if self.usage_pct is None:
            return "error"
        if self.usage_pct >= 90.0:
            return "bad"
        if self.usage_pct >= 70.0:
            return "warn"
        return "ok"

    def value_text(self) -> str:
        shown = self.pct_display
        if shown is None:
            return "—"
        return f"{shown}%"

    def reset_line(self) -> Optional[str]:
        """The clock that belongs to this allowance, then any short note."""
        parts: list[str] = []
        if self.resets:
            parts.append(f"resets {self.resets}")
        if self.detail:
            parts.append(self.detail)
        return " · ".join(parts) if parts else None

    def menu_text(self) -> str:
        if self.resets:
            return f"{self.label}  {self.value_text()} · {self.resets}"
        if self.detail:
            return f"{self.label}  {self.value_text()}"
        return f"{self.label}  {self.value_text()}"

    def summary_text(self) -> str:
        line = f"{self.label}: {self.value_text()}"
        extra = self.reset_line()
        if extra:
            return f"{line} · {extra}"
        return line


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
    products: tuple[UsageSlice, ...] = ()
    bot: Optional[UsageSlice] = None
    error: Optional[str] = None

    @property
    def usage_pct_display(self) -> int:
        """Floor to match Build SpendingLimiter truncation."""
        return pct_display(self.usage_pct)

    def plan_name(self) -> str:
        """Subscription that covers the shared week and grants Grok Bot."""
        if self.bot is not None and self.bot.plan:
            return self.bot.plan
        if self.subscription_tier:
            return self.subscription_tier
        return "SuperGrok"

    def breakdown_text(self) -> str:
        """How the shared week was spent. Unlisted Chat, Imagine, and Voice are 0."""
        spent = {"Build": 0.0, "Chat": 0.0, "Imagine": 0.0, "Voice": 0.0}
        seen: set[str] = set()
        for product in self.products:
            name = _SHARED_SLICE.get(product.label)
            if name is None or product.usage_pct is None:
                continue
            spent[name] = product.usage_pct
            seen.add(name)
        if "Build" not in seen and not seen:
            spent["Build"] = self.usage_pct
        return " · ".join(f"{name} {pct_display(spent[name])}%" for name in ("Build", "Chat", "Imagine", "Voice"))

    def plan_note(self) -> str:
        """Product mix under the plan, then credits when they exist."""
        extra = self.account_footnote()
        mix = self.breakdown_text()
        if extra:
            return f"{mix} · {extra}"
        return mix

    def pools(self) -> list[UsageSlice]:
        """The shared SuperGrok week, then any other product on that bill, then Grok Bot.

        Chat, Imagine, and Voice stay in the week's note instead of their own
        rows. Grok Bot keeps the separate clock from its own usage status.
        """
        if self.error:
            rows = [UsageSlice(label="Grok Build", usage_pct=None, detail=self.error)]
        else:
            rows = [
                UsageSlice(
                    label=self.plan_name(),
                    usage_pct=self.usage_pct,
                    resets=self.period_end_display,
                )
            ]
            for product in self.products:
                if product.label in _SHARED_ROW_LABELS or product.label == "Grok Bot":
                    continue
                rows.append(
                    UsageSlice(
                        label=product.label,
                        usage_pct=product.usage_pct,
                        resets=self.period_end_display,
                    )
                )
        if self.bot is not None:
            rows.append(self.bot)
        return rows

    def account_footnote(self) -> Optional[str]:
        """Credits and pay-as-you-go for the Build pool. Not a second clock."""
        parts: list[str] = []
        prepaid = self.prepaid_display()
        if prepaid:
            parts.append(f"credits {prepaid}")
            if self.auto_topup_enabled is True and self.auto_topup_amount_cents is not None:
                parts.append(f"auto top-up {_fmt_dollars(self.auto_topup_amount_cents)}")
            elif self.auto_topup_enabled is False:
                parts.append("auto top-up off")
        if self.pay_as_you_go:
            used = abs(self.on_demand_used_cents or 0)
            cap = abs(self.on_demand_cap_cents or 0)
            parts.append(f"pay as you go {_fmt_dollars(used)} / {_fmt_dollars(cap)}")
        return " · ".join(parts) if parts else None

    def menu_lines(self) -> list[str]:
        """Short lines for the menu-bar menu."""
        return [sl.menu_text() for sl in self.pools()]

    def menu_title(self) -> str:
        if self.error:
            return "GBU · ?"
        title = f"GBU · {self.usage_pct_display}%"
        bot_pct = self.bot.pct_display if self.bot is not None else None
        if bot_pct is not None:
            title = f"{title} · Bot {bot_pct}%"
        return title

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
        lines = []
        plan = self.plan_name()
        for sl in self.pools():
            text = sl.summary_text()
            if sl.label == plan and not self.error:
                text = f"{text} · {self.breakdown_text()}"
            lines.append(text)
        if self.error:
            lines.append("Run `grok login` if auth expired.")
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


def _products_from_config(config: dict[str, Any]) -> tuple[UsageSlice, ...]:
    raw = config.get("productUsage") or config.get("product_usage") or []
    if not isinstance(raw, list):
        return ()
    slices: list[UsageSlice] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("product") or item.get("name") or ""
        if not isinstance(name, str) or not name.strip():
            continue
        pct_raw = item.get("usagePercent")
        if pct_raw is None:
            pct_raw = item.get("usage_percent")
        try:
            pct = max(0.0, min(100.0, float(pct_raw)))
        except (TypeError, ValueError):
            continue
        slices.append(UsageSlice(label=product_label(name), usage_pct=pct))
    return tuple(slices)


def snapshot_from_billing(
    payload: dict[str, Any],
    *,
    auto_topup: Optional[dict[str, Any]] = None,
    bot: Optional[UsageSlice] = None,
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
            products=(),
            bot=bot,
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
        products=_products_from_config(config),
        bot=bot,
        error=None,
    )
