from django.conf import settings
from django.db import models
from django.utils.timezone import now
from datetime import timedelta
from django.db.models import F


UPLOAD_LIMITS = {"guest": 2, "basic": 3, "premium": 25, "pro": 100}

PLAN_CHOICES = (("basic", "Basic"), ("premium", "Premium"), ("pro", "Pro"))
CURRENCY_CHOICES = (("INR", "Indian Rupee"), ("USD", "US Dollar"), ("EUR", "Euro"))

# imports at top
from django.utils import timezone
from django.core.cache import cache
# from .pricing import clear_pricing_cache  # already present

class PlanPricing(models.Model):
    plan_type = models.CharField(max_length=20, choices=PLAN_CHOICES)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES)
    price_minor = models.PositiveIntegerField(help_text="Price in minor units (paise/cents)")
    duration_months = models.PositiveSmallIntegerField(default=1, help_text="Duration (months) applied after payment")

    class Meta:
        unique_together = ("plan_type", "currency")
        verbose_name = "Plan pricing"
        verbose_name_plural = "Plan pricing"

    def __str__(self):
        return f"{self.plan_type} — {self.currency}: {self.price_minor} ({self.duration_months}m)"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.clear()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        cache.clear()

# NEW: Limited-time offers that override price + duration
class LimitedTimeOffer(models.Model):
    PLAN_CHOICES = (("basic", "Basic"), ("premium", "Premium"), ("pro", "Pro"))

    plan_type = models.CharField(max_length=20, choices=PLAN_CHOICES)
    currency = models.CharField(max_length=3, default="INR")

    # Offer price in minor units (e.g., paise)
    price_minor = models.PositiveIntegerField()

    # Duration in DAYS for the offer (e.g., 7 days). If you prefer months, keep this at 0.
    duration_days = models.PositiveIntegerField(default=0)

    # Active window (leave ends_at null to run indefinitely until you toggle is_active)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)

    # Admin switches
    is_active = models.BooleanField(default=True)
    priority = models.SmallIntegerField(default=0)  # higher wins if overlaps

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["plan_type", "currency", "is_active", "starts_at", "ends_at"]),
        ]

    def __str__(self):
        window = f"{self.starts_at:%Y-%m-%d} → {self.ends_at:%Y-%m-%d}" if self.ends_at else f"{self.starts_at:%Y-%m-%d} → ∞"
        return f"Offer {self.plan_type}/{self.currency}: {self.price_minor} for {self.duration_days}d ({window})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # bust pricing caches
        cache.clear()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        cache.clear()


class SubscriptionPlan(models.Model):
    """Per-user subscription state."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription",
        null=True,
        blank=True,
    )
    plan_type = models.CharField(max_length=20, choices=PLAN_CHOICES, default="basic")
    upload_limit = models.PositiveIntegerField(null=True, blank=True)
    priority = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Subscription Plan"
        verbose_name_plural = "Subscription Plans"

    def __str__(self):
        target = self.user.username if self.user else "GLOBAL"
        return f"{target} — {self.plan_type}"

    def save(self, *args, **kwargs):
        if not self.upload_limit:
            self.upload_limit = UPLOAD_LIMITS.get(self.plan_type.lower(), 2)
        if self.plan_type == "basic":
            self.expires_at = None
        super().save(*args, **kwargs)

    def is_active(self):
        if self.expires_at:
            if now().date() > self.expires_at:
                self.ensure_current()
                return False
            return True
        return True

    @property
    def limit(self):
        return self.upload_limit or UPLOAD_LIMITS.get(self.plan_type, UPLOAD_LIMITS["basic"])

    def ensure_current(self, *, save=True):
        """Downgrade to the basic plan if the subscription has expired."""
        if self.expires_at and now().date() > self.expires_at:
            self.plan_type = "basic"
            self.upload_limit = UPLOAD_LIMITS["basic"]
            self.priority = 0
            self.expires_at = None
            if save:
                self.save(
                    update_fields=[
                        "plan_type",
                        "upload_limit",
                        "priority",
                        "expires_at",
                        "updated_at",
                    ]
                )
            return True
        return False


class ResumeUpload(models.Model):
    filename = models.CharField(max_length=512, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    session_id = models.CharField(max_length=128, null=True, blank=True)
    file = models.FileField(upload_to="resumes/%Y/%m/%d/")
    filename = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    file_size = models.PositiveIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=100, blank=True)

    def save(self, *args, **kwargs):
        if self.file and not self.filename:
            self.filename = self.file.name
        try:
            self.file_size = self.file.size
        except Exception:
            pass
        super().save(*args, **kwargs)

    def __str__(self):
        owner = self.user.username if self.user else f"session:{self.session_id}"
        return f"{owner} - {self.filename or 'resume'}"

class ResumeUploadCounter(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=128, null=True, blank=True)
    month = models.DateField(help_text="First day of month for grouping (e.g. 2025-09-01)")
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("user", "session_id", "month")
        indexes = [models.Index(fields=["user", "month"]), models.Index(fields=["session_id", "month"])]

    def __str__(self):
        owner = self.user.username if self.user else f"session:{self.session_id}"
        return f"{owner} - {self.month.strftime('%Y-%m')} : {self.count}"

    def reset_if_new_month(self):
        current_month_start = now().date().replace(day=1)
        if self.month != current_month_start:
            self.month = current_month_start
            self.count = 0
            self.save()

    def increment(self, delta=1):
        type(self).objects.filter(pk=self.pk).update(count=F('count') + delta)
        self.refresh_from_db(fields=['count'])   # <-- ensures 'count' is an int
        return self.count

    @classmethod
    def get_or_create_for(cls, user=None, session_id=None):
        month_start = now().date().replace(day=1)
        obj, created = cls.objects.get_or_create(user=user, session_id=session_id, month=month_start, defaults={"count": 0})
        if not created:
            obj.reset_if_new_month()
        return obj

class Payment(models.Model):
    PROVIDER_CHOICES = (("stripe", "Stripe"), ("razorpay", "Razorpay"), ("paypal", "PayPal"))
    STATUS_CHOICES = (("created", "Created"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("canceled", "Canceled"))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    plan_type = models.CharField(max_length=20, choices=PLAN_CHOICES)
    amount = models.PositiveIntegerField(help_text="Amount in minor units (paise/cents)")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="INR")
    country = models.CharField(max_length=2, blank=True)
    external_id = models.CharField(max_length=128, blank=True, help_text="PaymentIntent/Order ID")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    razorpay_order_id = models.CharField(max_length=128, blank=True)
    razorpay_payment_id = models.CharField(max_length=128, blank=True)
    razorpay_signature = models.CharField(max_length=256, blank=True)
    duration_months = models.PositiveIntegerField(null=True, blank=True)
    duration_days = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} - {self.provider} {self.status} {self.amount/100:.2f} {self.currency}"