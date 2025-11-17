from django.contrib import admin
from .models import SubscriptionPlan, ResumeUpload, ResumeUploadCounter, Payment

@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("user", "plan_type", "upload_limit", "priority", "expires_at", "created_at")
    list_filter = ("plan_type", "expires_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at", "updated_at")

@admin.register(ResumeUpload)
class ResumeUploadAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "filename", "created_at", "file_size")
    search_fields = ("filename", "user__username", "session_id")
    list_filter = ("created_at",)

    def owner(self, obj):
        return obj.user.username if obj.user else obj.session_id

@admin.register(ResumeUploadCounter)
class ResumeUploadCounterAdmin(admin.ModelAdmin):
    list_display = ("owner", "month", "count")
    search_fields = ("user__username", "session_id")
    list_filter = ("month",)

    def owner(self, obj):
        return obj.user.username if obj.user else obj.session_id

# @admin.register(Payment)
# class PaymentAdmin(admin.ModelAdmin):
#     list_display = ("user", "provider", "plan_type", "amount", "currency", "status", "created_at")
#     list_filter = ("provider", "status", "currency", "plan_type")
#     search_fields = ("user__username", "external_id")

from django.contrib import admin
from .models import LimitedTimeOffer

@admin.register(LimitedTimeOffer)
class LimitedTimeOfferAdmin(admin.ModelAdmin):
    list_display = ("plan_type", "currency", "price_minor", "duration_days", "is_active", "starts_at", "ends_at", "priority")
    list_filter = ("plan_type", "currency", "is_active")
    search_fields = ("plan_type", "currency")
    ordering = ("-is_active", "-priority", "-starts_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "provider",
        "plan_type",
        "amount",
        "currency",
        "duration_months",
        "duration_days",     # NEW: show offer days if any
        "status",
        "created_at",
    )
    list_filter = ("provider", "status", "currency", "plan_type")
    search_fields = ("user__username", "external_id")