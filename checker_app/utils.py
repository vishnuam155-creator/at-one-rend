# checker_app/utils.py
from datetime import timedelta
from .models import PLAN_CHOICES, SubscriptionPlan, UPLOAD_LIMITS
from django.utils.timezone import now

VALID_PLAN_TYPES = {name for name, _ in PLAN_CHOICES}

def apply_subscription(user, plan_type: str, months: int = 1, days: int = 0):
    """Upgrade/extend subscription with precise months and days."""
    normalized = (plan_type or "").strip().lower()
    if not normalized:
        raise ValueError("plan_type is required to apply a subscription")
    if normalized not in VALID_PLAN_TYPES:
        raise ValueError(f"Unsupported plan_type: {plan_type}")

    # sanitize
    months = int(months or 0)
    days = int(days or 0)
    if months <= 0 and days <= 0:
        months = 1  # default to 1 month to avoid accidental 0

    sub, _ = SubscriptionPlan.objects.get_or_create(user=user)
    sub.plan_type = normalized
    sub.upload_limit = UPLOAD_LIMITS.get(normalized, UPLOAD_LIMITS["basic"])

    if normalized == "basic":
        sub.expires_at = None
    else:
        base = now().date()
        if sub.expires_at and sub.expires_at > base:
            base = sub.expires_at  # extend from current expiry (stackable)
        # months approximated as 30-day blocks to stay dependency-free
        delta_days = (30 * months) + days
        sub.expires_at = base + timedelta(days=delta_days)

    sub.save()
    if hasattr(user, "_subscription_cache"):
        user._subscription_cache = sub
    return sub



def get_active_subscription(user):
    """Return the user's subscription after enforcing expiry rules."""
    if not getattr(user, "is_authenticated", False):
        return None

    sub = SubscriptionPlan.objects.filter(user=user).first()
    if not sub:
        return None

    # Update any cached relation before and after applying expiry rules so we
    # don't operate on stale data (which could immediately downgrade).
    if hasattr(user, "_subscription_cache"):
        user._subscription_cache = sub

    sub.ensure_current()

    if hasattr(user, "_subscription_cache"):
        user._subscription_cache = sub
    return sub
