#!/usr/bin/env python3
"""DeepSeek list prices, so token counts can be turned into an honest cost.

Both tables hold DeepSeek's own published numbers per 1M tokens: the CNY table
comes from the Chinese pricing page, the USD table from the English one. CNY is
the default and is *not* a converted guess — converting one currency into the
other with an invented exchange rate would put a wrong number on screen.

Two things must not be flattened away:

* Cache hit, cache miss and output have different prices, and the logged
  `input_tokens` already includes the cached part, so the two must be split.
* The rate depends on when the request ran. Off-peak is exactly half of peak;
  peak is 09:00-12:00 and 14:00-18:00 Beijing time (01:00-04:00 and 06:00-10:00
  UTC), Monday to Friday, excluding Chinese public holidays. Everything else —
  including whole weekends — is off-peak. So cost is per response, never a
  single multiplier on a day's total.

Nothing here talks to the network. Update the tables when DeepSeek changes
prices; the widget and the CLI both read them.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

DEFAULT_CURRENCY = "CNY"

# currency -> model -> (cache hit, cache miss, output) per 1M tokens at PEAK rates
PEAK_RATES: dict[str, dict[str, tuple[float, float, float]]] = {
    "CNY": {  # 元/百万 tokens，DeepSeek 中文定价页
        "deepseek-flash": (0.04, 2.0, 8.0),
        "deepseek-v4-pro": (0.30, 9.0, 27.0),
    },
    "USD": {  # DeepSeek 英文定价页
        "deepseek-flash": (0.006, 0.30, 1.20),
        "deepseek-v4-pro": (0.044, 1.32, 3.96),
    },
}

SYMBOLS = {"CNY": "¥", "USD": "$"}

# retired names that DeepSeek still accepts and bills at the Flash price
ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4": "deepseek-v4-pro",
}

OFF_PEAK_FACTOR = 0.5
PEAK_HOURS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))  # [start, stop)


def normalize_currency(currency: str | None) -> str:
    """A currency this table can price in, falling back to the default."""
    name = (currency or DEFAULT_CURRENCY).upper()
    return name if name in PEAK_RATES else DEFAULT_CURRENCY


def resolve(model: str | None) -> str | None:
    """Canonical model name, or None when this table has no price for it."""
    if not model:
        return None
    name = ALIASES.get(model, model)
    return name if name in PEAK_RATES[DEFAULT_CURRENCY] else None


def rates_for(model: str | None, currency: str = DEFAULT_CURRENCY) \
        -> tuple[float, float, float] | None:
    """Peak rates for a model, or None when it has no entry in this table."""
    name = resolve(model)
    return PEAK_RATES[normalize_currency(currency)][name] if name else None


def load_holidays() -> set[str]:
    """Chinese public holidays (local dates) that make a peak window off-peak.

    Not shipped as data: the dates change every year and a wrong list would
    silently misprice. Set CODEX_USAGE_HOLIDAYS to a comma-separated list of
    YYYY-MM-DD local dates to enable the exemption.
    """
    raw = os.environ.get("CODEX_USAGE_HOLIDAYS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_peak(timestamp: str | None, holidays: set[str] | None = None) -> bool:
    """True when this UTC log timestamp falls in a DeepSeek peak window."""
    if not timestamp:
        return False
    try:
        utc = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if utc.tzinfo is None:  # logs are UTC; a naive stamp is UTC too
        utc = utc.replace(tzinfo=timezone.utc)
    else:
        utc = utc.astimezone(timezone.utc)
    local = utc.astimezone()  # holidays are named as local dates
    if local.date().isoformat() in (holidays if holidays is not None else load_holidays()):
        return False
    if utc.weekday() >= 5:  # Saturday and Sunday are entirely off-peak
        return False
    hour = utc.hour
    return any(start <= hour < stop for start, stop in PEAK_HOURS_UTC)


def cost_of(usage: dict, model: str | None, timestamp: str | None,
            assumed: str | None = None, holidays: set[str] | None = None,
            currency: str = DEFAULT_CURRENCY) -> float | None:
    """Price of one logged response in the chosen currency, or None when unknown.

    `assumed` keeps a live readout from silently showing 0 for a model the
    table does not know: pass the session's model, and call `resolve(model)` to
    tell an exact price from an assumed one.
    """
    rates = rates_for(model, currency) or rates_for(assumed, currency)
    if rates is None:
        return None
    hit_rate, miss_rate, out_rate = rates
    cached = int(usage.get("cached_input_tokens") or 0)
    uncached = max(int(usage.get("input_tokens") or 0) - cached, 0)
    output = int(usage.get("output_tokens") or 0)
    factor = 1.0 if is_peak(timestamp, holidays) else OFF_PEAK_FACTOR
    amount = (cached * hit_rate + uncached * miss_rate + output * out_rate) * factor
    return amount / 1_000_000


def money(value: float | None, currency: str = DEFAULT_CURRENCY) -> str:
    """`¥0.53` / `$0.53`; four decimals while the amount is still tiny."""
    if value is None:
        return ""
    symbol = SYMBOLS[normalize_currency(currency)]
    digits = 2 if abs(value) >= 0.01 else 4
    return f"{symbol}{value:,.{digits}f}"


def describe(currency: str = DEFAULT_CURRENCY) -> str:
    """One-line price list for logs, prompts and the widget's help dialog."""
    name = normalize_currency(currency)
    symbol = SYMBOLS[name]
    parts = []
    for model, (hit, miss, out) in PEAK_RATES[name].items():
        parts.append(f"{model}: 命中{symbol}{hit:g}/未命中{symbol}{miss:g}"
                     f"/输出{symbol}{out:g} 每1M（高峰，空闲减半）")
    return "；".join(parts)
