# checker_app/serializers.py

from datetime import datetime, time as dtime
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import (
    SubscriptionPlan,
    ResumeUpload,
    ResumeUploadCounter,
    Payment,
    UPLOAD_LIMITS,
)
from .utils import get_active_subscription


# -----------------------
# Core model serializers
# -----------------------
class SubscriptionPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = (
            "id",
            "user",
            "plan_type",
            "upload_limit",
            "priority",
            "expires_at",
            "created_at",
            "updated_at",
        )


class ResumeUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResumeUpload
        fields = ("id", "user", "session_id", "file", "filename", "created_at")


class ResumeUploadCounterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResumeUploadCounter
        fields = ("id", "user", "session_id", "month", "count")


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = (
            "id",
            "user",
            "provider",
            "plan_type",
            "amount",
            "currency",
            "country",
            "duration_months",
            "external_id",
            "status",
            "created_at",
        )


User = get_user_model()


# -----------------------
# Profile serializer (+expiry normalization)
# -----------------------
class UserProfileSerializer(serializers.ModelSerializer):
    plan = serializers.SerializerMethodField()
    uploads_used = serializers.SerializerMethodField()
    upload_limit = serializers.SerializerMethodField()
    is_email_verified = serializers.SerializerMethodField()

    # Expose subscription expiry/state
    plan_expires_at = serializers.SerializerMethodField()
    plan_expires_in_days = serializers.SerializerMethodField()
    is_plan_active = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "plan",
            "uploads_used",
            "upload_limit",
            "date_joined",
            "is_email_verified",
            # expiry info:
            "plan_expires_at",
            "plan_expires_in_days",
            "is_plan_active",
        ]

    # Avoid multiple DB hits for the same user
    def _get_subscription(self, obj):
        return get_active_subscription(obj)

    # --- Basic profile fields ---
    def get_plan(self, obj):
        sub = self._get_subscription(obj)
        return sub.plan_type if sub else "basic"

    def get_upload_limit(self, obj):
        sub = self._get_subscription(obj)
        return getattr(sub, "limit", None) or UPLOAD_LIMITS["guest"]

    def get_uploads_used(self, obj):
        counter = ResumeUploadCounter.get_or_create_for(user=obj)
        return counter.count

    def get_is_email_verified(self, obj):
        return obj.is_active

    # --- Expiry normalization helpers/fields ---
    def _expiry_as_aware_datetime(self, expires):
        """
        Accepts either a date or datetime from SubscriptionPlan.expires_at.
        Returns a timezone-aware datetime at end-of-day for dates.
        """
        if not expires:
            return None

        # If it's already a datetime, ensure it's tz-aware
        if isinstance(expires, datetime):
            if timezone.is_naive(expires):
                return timezone.make_aware(expires, timezone.get_current_timezone())
            return expires

        # Treat dates as local end-of-day
        dt = datetime.combine(expires, dtime.max)
        return timezone.make_aware(dt, timezone.get_current_timezone())

    def get_plan_expires_at(self, obj):
        sub = self._get_subscription(obj)
        if not sub or not getattr(sub, "expires_at", None):
            return None
        expires_dt = self._expiry_as_aware_datetime(sub.expires_at)
        return expires_dt.isoformat() if expires_dt else None

    def get_plan_expires_in_days(self, obj):
        sub = self._get_subscription(obj)
        if not sub or not getattr(sub, "expires_at", None):
            return None
        expires_dt = self._expiry_as_aware_datetime(sub.expires_at)
        if not expires_dt:
            return None
        remaining = expires_dt - timezone.now()
        return max(0, remaining.days)

    def get_is_plan_active(self, obj):
        sub = self._get_subscription(obj)
        if not sub or not getattr(sub, "expires_at", None):
            return False
        expires_dt = self._expiry_as_aware_datetime(sub.expires_at)
        return bool(expires_dt and expires_dt > timezone.now())


# -----------------------
# Resume payload serializers
# -----------------------
class ContactInfoSerializer(serializers.Serializer):
    firstName = serializers.CharField()
    lastName = serializers.CharField()
    email = serializers.EmailField()
    phone = serializers.CharField(allow_blank=True, required=False)
    location = serializers.CharField(allow_blank=True, required=False)
    website = serializers.CharField(allow_blank=True, required=False)
    linkedin = serializers.CharField(allow_blank=True, required=False)
    github = serializers.CharField(allow_blank=True, required=False)


class ExperienceSerializer(serializers.Serializer):
    id = serializers.CharField()
    jobTitle = serializers.CharField()
    company = serializers.CharField()
    location = serializers.CharField(allow_blank=True, required=False)
    startDate = serializers.CharField()
    endDate = serializers.CharField(allow_blank=True, required=False)  # may be ""
    isCurrentJob = serializers.BooleanField()
    description = serializers.CharField(allow_blank=True, required=False)  # rich HTML or plain


class EducationSerializer(serializers.Serializer):
    id = serializers.CharField()
    institution = serializers.CharField()
    degree = serializers.CharField()
    fieldOfStudy = serializers.CharField(allow_blank=True, required=False)
    startDate = serializers.CharField()
    endDate = serializers.CharField(allow_blank=True, required=False)  # may be ""
    isCurrentlyStudying = serializers.BooleanField()
    gpa = serializers.CharField(allow_blank=True, required=False)
    description = serializers.CharField(allow_blank=True, required=False)  # rich HTML or plain


class CertificateSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    issuer = serializers.CharField()
    issueDate = serializers.CharField()
    expirationDate = serializers.CharField(allow_blank=True, required=False)
    credentialId = serializers.CharField(allow_blank=True, required=False)
    url = serializers.CharField(allow_blank=True, required=False)


class ProjectSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False)  # rich HTML or plain
    technologies = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    startDate = serializers.CharField()
    endDate = serializers.CharField(allow_blank=True, required=False)  # may be ""
    isOngoing = serializers.BooleanField()
    url = serializers.CharField(allow_blank=True, required=False)
    githubUrl = serializers.CharField(allow_blank=True, required=False)


class SkillSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    level = serializers.ChoiceField(
        choices=["Beginner", "Intermediate", "Advanced", "Expert", "Not specified"]
    )
    category = serializers.CharField()


class ResumeDataSerializer(serializers.Serializer):
    contacts = ContactInfoSerializer()
    summary = serializers.CharField(allow_blank=True)  # rich HTML or plain
    experience = ExperienceSerializer(many=True)
    education = EducationSerializer(many=True)
    certificates = CertificateSerializer(many=True)
    projects = ProjectSerializer(many=True)
    skills = SkillSerializer(many=True)
    photo = serializers.CharField(allow_blank=True, required=False)  # base64 when with-photo


class GeneratePayloadSerializer(serializers.Serializer):
    template = serializers.ChoiceField(choices=[
        "professional", "modern", "creative", "new",
        "executive", "technical", "minimalist", "elegant",
        "bold", "compact", "academic", "startup",
        "classic", "colorful"
    ])
    format = serializers.ChoiceField(choices=["with-photo", "without-photo"])
    data = ResumeDataSerializer()
from rest_framework import serializers
from .models import LimitedTimeOffer
from .models import PlanPricing

class PlanPricingSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanPricing
        fields = ["id", "plan_type", "currency", "price_minor", "duration_months"]


class LimitedTimeOfferSerializer(serializers.ModelSerializer):
    class Meta:
        model = LimitedTimeOffer
        fields = [
            "id",
            "plan_type",
            "currency",
            "price_minor",
            "duration_days",
            "starts_at",
            "ends_at",
            "is_active",
            "priority",
            "created_at",
            "updated_at",
        ]
