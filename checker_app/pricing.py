"""Helpers for subscription pricing configuration and limited-time offers."""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, Tuple

from django.apps import apps
from django.core.cache import cache
from django.utils import timezone

# ---- Defaults in case DB rows are missing ----
DEFAULT_PRICE_TABLE: Dict[str, Dict[str, int]] = {
    "INR": {"basic": 0, "premium": 30000, "pro": 65000},
    "USD": {"basic": 0, "premium": 500, "pro": 900},
    # "EUR": {"basic": 0, "premium": 499, "pro": 1199},
}
DEFAULT_DURATION_MONTHS = 1
CACHE_TTL = 60  # seconds

# ---- Lazy getters (avoid circular imports) ----
def _PlanPricing():
    return apps.get_model("checker_app", "PlanPricing")

def _LimitedTimeOffer():
    return apps.get_model("checker_app", "LimitedTimeOffer")

# ---- Public: clear caches when Admin edits ----
def clear_pricing_cache():
    cache.clear()
    load_pricing_tables.cache_clear()
    get_prices_for_currency.cache_clear()
    get_price_for_plan.cache_clear()
    get_plan_duration.cache_clear()

# ---- Merge DB overrides with defaults ----
def _merge_defaults(prices: Dict[str, Dict[str, int]],
                    durations: Dict[str, Dict[str, int]]):
    merged_prices: Dict[str, Dict[str, int]] = {}
    merged_durations: Dict[str, Dict[str, int]] = {}
    for currency, plans in DEFAULT_PRICE_TABLE.items():
        merged_prices[currency] = plans.copy()
        merged_durations[currency] = {plan: DEFAULT_DURATION_MONTHS for plan in plans}
    for currency, plans in prices.items():
        merged_prices.setdefault(currency, {})
        merged_durations.setdefault(currency, {})
        merged_prices[currency].update(plans)
        for plan, duration in durations.get(currency, {}).items():
            merged_durations[currency][plan] = duration or DEFAULT_DURATION_MONTHS
    return merged_prices, merged_durations

@lru_cache(maxsize=1)
def load_pricing_tables():
    PlanPricing = _PlanPricing()
    prices: Dict[str, Dict[str, int]] = {}
    durations: Dict[str, Dict[str, int]] = {}
    for row in PlanPricing.objects.all():
        prices.setdefault(row.currency, {})[row.plan_type] = row.price_minor
        durations.setdefault(row.currency, {})[row.plan_type] = row.duration_months
    return _merge_defaults(prices, durations)

@lru_cache(maxsize=64)
def get_prices_for_currency(currency: str) -> Dict[str, int]:
    prices, _ = load_pricing_tables()
    currency = (currency or "").upper()
    return prices.get(currency) or prices.get("USD", {})

@lru_cache(maxsize=256)
def get_price_for_plan(plan_type: str, currency: str) -> int:
    plan_type = (plan_type or "").lower()
    if not plan_type:
        raise ValueError("plan_type is required")
    table = get_prices_for_currency(currency)
    if plan_type in table:
        return table[plan_type]
    usd = load_pricing_tables()[0].get("USD", {})
    if plan_type in usd:
        return usd[plan_type]
    raise ValueError(f"Unknown pricing for plan '{plan_type}'")

@lru_cache(maxsize=256)
def get_plan_duration(plan_type: str, currency: str) -> int:
    plan_type = (plan_type or "").lower()
    if not plan_type:
        return DEFAULT_DURATION_MONTHS
    _, durations = load_pricing_tables()
    currency = (currency or "").upper()
    return (durations.get(currency) or durations.get("USD", {})).get(plan_type, DEFAULT_DURATION_MONTHS)

# ---- Offers ----
from django.utils import timezone
from django.core.cache import cache
from datetime import timedelta

from .models import PlanPricing, LimitedTimeOffer

CACHE_TTL = 60  # seconds; tweak as you like

def _active_offer(plan_type: str, currency: str):
    """
    Return the best active LimitedTimeOffer (or None).
    Priority: is_active, time window matches now, highest priority wins, newest wins.
    Cached briefly for speed.
    """
    key = f"offer:{plan_type}:{currency}"
    hit = cache.get(key)
    if hit is not None:
        return hit

    now = timezone.now()
    qs = (LimitedTimeOffer.objects
          .filter(plan_type=plan_type, currency=currency, is_active=True, starts_at__lte=now)
          .order_by("-priority", "-starts_at", "-id"))
    if qs.exists():
        obj = qs.first()
        # respect ends_at if set
        if obj.ends_at is None or obj.ends_at >= now:
            cache.set(key, obj, CACHE_TTL)
            return obj

    cache.set(key, None, CACHE_TTL)
    return None


def get_effective_price_and_duration(plan_type: str, currency: str):
    plan_type = (plan_type or "").lower()
    currency = (currency or "").upper()

    offer = _active_offer(plan_type, currency)
    if offer:
        return offer.price_minor, 0, (offer.duration_days or 0), "offer"

    # fall back to standard plan pricing
    price = get_price_for_plan(plan_type, currency)  # keep your existing helper if it doesn't import models at top
    months = get_plan_duration(plan_type, currency)  # same note as above
    return price, months, 0, "plan"

