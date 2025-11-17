# checker_app/views.py
from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import re
import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, List, Tuple, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.mail import send_mail
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt

from dotenv import load_dotenv
from httpcore import request
from markdown import markdown as md
from PIL import Image
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from weasyprint import HTML, CSS

import google.generativeai as genai
import pdfplumber
import pytesseract
import stripe
from pdf2image import convert_from_bytes
import pdf2image  # keep: used for resume_checker
import pypdfium2 as pdfium

from firebase_admin import auth as firebase_auth

from .models import (
    SubscriptionPlan,
    ResumeUploadCounter,
    ResumeUpload,
    UPLOAD_LIMITS,
    Payment,
)
from .serializers import UserProfileSerializer, GeneratePayloadSerializer
from .utils import apply_subscription, get_active_subscription
from .pricing import get_prices_for_currency, get_price_for_plan, get_plan_duration
from .firebase_utils import ensure_firebase_initialized, FirebaseInitError

# -----------------------------------------------------------------------------
# Logging & env
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)
load_dotenv()

# checker_app/views.py
from django.http import JsonResponse

def home(request):
    return JsonResponse({"status": "ok", "message": "ATS Resume API is running"})



# -----------------------------------------------------------------------------
# Gemini failover client
# -----------------------------------------------------------------------------
@dataclass
class GeminiKeyRing:
    """Holds two keys and falls back automatically if the first fails."""
    primary: Optional[str]
    secondary: Optional[str]
    single_fallback: Optional[str] = None
    _last_good: int = 0  # 0=primary, 1=secondary, 2=single_fallback

    def keys_in_order(self) -> List[str]:
        order: List[str] = []
        if self.primary:
            order.append(self.primary)
        if self.secondary:
            order.append(self.secondary)
        if self.single_fallback:
            order.append(self.single_fallback)

        # rotate so last_good is first
        if order:
            self._last_good = min(self._last_good, len(order) - 1)
            order = order[self._last_good :] + order[: self._last_good]
        return order

    def mark_good(self, key_index: int):
        self._last_good = key_index


class GeminiClient:
    """Small wrapper that configures the SDK with a working key (with failover)."""

    def __init__(self, model_name: str = "gemini-2.0-flash"):
        self.model_name = model_name
        self.keys = GeminiKeyRing(
            primary=os.getenv("GOOGLE_API_KEY_PRIMARY"),
            secondary=os.getenv("GOOGLE_API_KEY_SECONDARY"),
            single_fallback=os.getenv("GOOGLE_API_KEY"),
        )

    def _configure(self, api_key: str):
        genai.configure(api_key=api_key)

    def get_model(self) -> genai.GenerativeModel:
        # Ensure we configured a usable key first
        for idx, k in enumerate(self.keys.keys_in_order()):
            try:
                self._configure(k)
                model = genai.GenerativeModel(self.model_name)
                # lightweight probe: a tiny non-billable check is not available,
                # so we rely on first call failures to trigger fallback.
                self.keys.mark_good(idx)
                return model
            except Exception as e:
                logger.warning("Gemini key idx=%s failed to init: %s", idx, e)
        raise RuntimeError("No valid Gemini API key configured.")

    def generate_text(self, parts: List[str | dict]) -> str:
        """parts can be [prompt, image-part, text] like your previous calls."""
        last_exc = None
        for idx, k in enumerate(self.keys.keys_in_order()):
            try:
                self._configure(k)
                model = genai.GenerativeModel(self.model_name)
                resp = model.generate_content(parts)

                feedback = getattr(resp, "prompt_feedback", None)
                if feedback is not None:
                    block_reason = getattr(feedback, "block_reason", None)
                    if block_reason:
                        raise RuntimeError(f"Gemini blocked prompt: {block_reason}")

                text = _extract_text_from_gemini_response(resp)
                if not text:
                    raise RuntimeError("Gemini returned an empty response")

                self.keys.mark_good(idx)
                return text
            except Exception as e:
                # last_exc = e
                logger.warning("call failed with key idx=%s: %s", idx, e)
        # If we got here, both keys failed
        raise RuntimeError(f"generation failed error")


gemini_client = GeminiClient(model_name="gemini-2.0-flash")


# -----------------------------------------------------------------------------
# Pricing & geo helpers
# -----------------------------------------------------------------------------
PRICE_TABLE = {
    "INR": {"basic": 0, "premium": 300, "pro": 650},
    "USD": {"basic": 0, "premium": 78, "pro": 140},
    "EUR": {"basic": 0, "premium": 499, "pro": 1199},
}
COUNTRY_TO_CURRENCY = {
    "IN": "INR",
    "US": "USD",
    "CA": "USD",
    "GB": "USD",
    "DE": "EUR",
    "FR": "EUR",
}


def detect_country(request) -> str:
    q_country = (request.GET.get("country") or (getattr(request, "data", {}) or {}).get("country"))
    if q_country:
        return q_country.upper()[:2]
    country = request.META.get("HTTP_CF_IPCOUNTRY") or request.META.get("GEOIP_COUNTRY_CODE")
    if country:
        return country.upper()
    return "IN" if settings.TIME_ZONE.endswith("Kolkata") else "US"


def get_plan_and_limit(user, session_id) -> Tuple[str, int]:
    if user:
        sub = get_active_subscription(user)
        if sub:
            return sub.plan_type.lower(), sub.limit
    return "guest", UPLOAD_LIMITS["guest"]

# -----------------------------------------------------------------------------
# Resume checker (Gemini image + text)
# -----------------------------------------------------------------------------
def input_pdf_setup(uploaded_file) -> List[dict]:
    pdf_bytes = uploaded_file.read()
    uploaded_file.seek(0)

    def _as_payload(image: Image.Image) -> List[dict]:
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format="JPEG")
        img_bytes = img_byte_arr.getvalue()
        # Gemini expects inline binary parts marks, not raw dicts
        return [
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(img_bytes).decode(),
                }
            }
        ]

    try:
        images = pdf2image.convert_from_bytes(pdf_bytes)
        return _as_payload(images[0])
    except pdf2image.exceptions.PDFInfoNotInstalledError:
        logger.warning("pdf2image poppler backend missing; falling back to pypdfium2")
    except Exception as exc:
        logger.warning("pdf2image failed (%s); attempting pypdfium2 fallback", exc)

    try:
        pdf_doc = pdfium.PdfDocument(pdf_bytes)
        page = pdf_doc.get_page(0)
        bitmap = page.render(scale=300 / 72)
        pil_image = bitmap.to_pil()
        bitmap.close()
        page.close()
        pdf_doc.close()
        return _as_payload(pil_image)
    except Exception as exc:
        logger.error("All PDF renderers failed: %s", exc)
        raise


def _extract_text_from_gemini_response(resp: Any) -> str:
    """Best-effort extraction of text from Gemini responses."""
    if not resp:
        return ""

    text = getattr(resp, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        # Skip blocked candidates when finish_reason advertises safety/none completion
        finish_reason = getattr(cand, "finish_reason", None)
        if isinstance(finish_reason, str) and finish_reason.upper() == "SAFETY":
            continue

        content = getattr(cand, "content", None)
        parts = []
        if content is not None:
            parts = getattr(content, "parts", None) or []
        if not parts:
            parts = getattr(cand, "parts", None) or []

        fragments: List[str] = []
        for part in parts:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                fragments.append(part_text.strip())

        candidate_text = "\n".join(fragments).strip()
        if candidate_text:
            return candidate_text

    return ""


def get_gemini_response(input_prompt: str, pdf_content: List[dict], job_desc: str) -> str:
    parts: List[dict] = []
    if input_prompt:
        parts.append({"text": input_prompt})

    parts.extend(pdf_content)

    if job_desc:
        parts.append({"text": f"Job Description:\n{job_desc}"})

    return gemini_client.generate_text(parts)


class ResumeScopeThrottle(ScopedRateThrottle):
    scope = "resume"


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
@throttle_classes([ResumeScopeThrottle])
def resume_checker(request):
    try:
        input_prompt_review = (
            "You are an experienced Technical Human Resource Manager.\n"
            "Review the provided resume against the job description.\n"
            "Share your professional evaluation, highlighting strengths and weaknesses."
        )

        input_prompt_ats = (
            """You are a skilled ATS (Applicant Tracking System) scanner with a deep understanding 
            of ATS functionality. Evaluate the resume against the provided job description. 
            Give me the percentage of match, then list missing keywords, and finally provide overall thoughts."""
        )

        user = request.user if request.user.is_authenticated else None
        if not user and not request.session.session_key:
            request.session.create()
        session_id = None if user else request.session.session_key

        counter, _ = ResumeUploadCounter.objects.get_or_create(
            user=user if user else None,
            session_id=session_id if not user else None,
            month=now().date().replace(day=1),
        )
        counter.reset_if_new_month()
        plan, limit = get_plan_and_limit(user, session_id)

        if request.method == "GET":
            return Response({"uploads_used": counter.count, "limit": limit, "plan": plan})

        uploaded_file = request.FILES.get("resume")
        if not uploaded_file:
            return Response({"error": "No resume uploaded"}, status=400)

        if counter.count >= limit:
            return Response(
                {"error": f"Upload limit reached for {plan} ({limit}). Upgrade to continue."},
                status=403,
            )

        # Extract inputs
        job_desc = (request.data.get("job_desc") or "").strip()
        company_name = (request.data.get("company_name") or "").strip()
        action = (request.data.get("action") or "review").strip().lower()

        # Parse resume content
        pdf_content = input_pdf_setup(uploaded_file)

        # If JD is missing but company name is given → generate JD dynamically
        if not job_desc and company_name:
            jd_prompt = (
                "You are a professional HR assistant. "
                f"Write a concise job description for a {company_name} role suitable for resume evaluation. "
                "Include typical responsibilities, skills, and qualifications."
            )
            job_desc = gemini_client.generate_text([jd_prompt, pdf_content[0], company_name])

        # Choose which evaluation to perform
        base_prompt = input_prompt_review if action == "review" else input_prompt_ats
        response_text = get_gemini_response(base_prompt, pdf_content, job_desc)

        # Save counters and logs
        counter.increment(1)
        ResumeUpload.objects.create(
            user=user if user else None,
            session_id=session_id if not user else None,
            file=uploaded_file,
            filename=uploaded_file.name,
        )

        return Response(
            {"response": response_text, "uploads_used": counter.count, "limit": limit, "plan": plan}
        )

    except Exception as e:
        logger.exception("resume_checker failed")
        if settings.DEBUG:
            return Response({"error": str(e)}, status=500)
        return Response(
            {"error": "Something went wrong while processing your request. Please try again later."},
            status=500,
        )


# -----------------------------------------------------------------------------
# Auth / Profile / Sync
# -----------------------------------------------------------------------------
@api_view(["POST"])
@permission_classes([AllowAny])
def sync_user(request):
    data = request.data
    email = (data.get("email") or "").strip().lower()
    if not email:
        return Response({"error": "email is required"}, status=400)
    user, _ = User.objects.get_or_create(username=email, defaults={"email": email})
    user.last_login = now()
    user.save()
    sub, created = SubscriptionPlan.objects.get_or_create(user=user, defaults={"plan_type": "basic"})
    sub.ensure_current()
    return Response({"email": email, "plan": sub.plan_type, "created": created})


@api_view(["POST"])
@permission_classes([AllowAny])
def get_user_plan(request):
    username = request.data.get("username")
    try:
        user = User.objects.get(username=username)
        sub = get_active_subscription(user)
        plan = sub.plan_type if sub else "guest"
    except Exception:
        plan = "guest"
    return JsonResponse({"username": username, "plan": plan})


@api_view(["POST"])
@permission_classes([AllowAny])
def firebase_login(request):
    id_token = request.data.get("idToken")
    if not id_token:
        return Response({"error": "ID token missing"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        ensure_firebase_initialized()
        decoded = firebase_auth.verify_id_token(id_token)
        email = decoded["email"]
        name = decoded.get("name", "")
        user, _ = User.objects.get_or_create(
            username=email, defaults={"email": email, "first_name": name}
        )
        token, _ = Token.objects.get_or_create(user=user)
        sub = get_active_subscription(user)
        plan = sub.plan_type if sub else "basic"
        return Response({"token": token.key, "username": user.username, "plan": plan})
    except FirebaseInitError as init_exc:
        logger.error("Firebase not initialized for login: %s", init_exc)
        return Response(
            {"error": "Firebase auth is not configured. Please contact support."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except Exception as e:
        logger.exception("firebase_login failed")
        if settings.DEBUG:
            return Response({"error": str(e)}, status=400)
        return Response(
            {"error": "Unable to complete Firebase login at this time. Please try again later."},
            status=400,
        )


UserModel = get_user_model()


@api_view(["POST"])
@permission_classes([AllowAny])
def firebase_email_login(request):
    id_token = request.data.get("idToken")
    if not id_token:
        return Response({"error": "ID token missing"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        ensure_firebase_initialized()
        decoded_token = firebase_auth.verify_id_token(id_token)
        firebase_uid = decoded_token["uid"]
        email = decoded_token["email"]
        firebase_user_record = firebase_auth.get_user(firebase_uid)
        if not firebase_user_record.email_verified:
            return Response(
                {"error": "Email not verified. Please check your inbox for a verification link."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user, created = UserModel.objects.get_or_create(
            email=email,
            defaults={
                "username": email,
                "first_name": (firebase_user_record.display_name or "").split(" ")[0]
                if firebase_user_record.display_name
                else "",
                "last_name": (firebase_user_record.display_name or "").split(" ")[-1]
                if firebase_user_record.display_name
                else "",
            },
        )
        if created:
            user.is_active = True
            user.set_unusable_password()
            user.save()
        token, _ = Token.objects.get_or_create(user=user)
        sub = get_active_subscription(user)
        plan = sub.plan_type if sub else "basic"
        return Response({"token": token.key, "username": user.username, "plan": plan}, status=200)
    except FirebaseInitError as init_exc:
        logger.error("Firebase not initialized for email login: %s", init_exc)
        return Response(
            {"error": "Firebase auth is not configured. Please contact support."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except Exception as e:
        logger.exception("firebase_email_login failed")
        if settings.DEBUG:
            return Response({"error": str(e)}, status=500)
        return Response(
            {"error": "Unable to complete Firebase email login at this time. Please try again later."},
            status=500,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def register_firebase_email(request):
    id_token = request.data.get("idToken")
    username = request.data.get("username")
    first_name = request.data.get("first_name", "")
    last_name = request.data.get("last_name", "")
    if not id_token or not username:
        return Response({"error": "ID token and username are required"}, status=400)
    try:
        decoded_token = firebase_auth.verify_id_token(id_token)
        email = decoded_token["email"]
        if UserModel.objects.filter(username=username).exists():
            return Response({"error": "A user with this username already exists."}, status=409)
        user, created = UserModel.objects.get_or_create(
            email=email,
            defaults={"username": username, "first_name": first_name, "last_name": last_name, "is_active": False},
        )
        if not created:
            user.username = username
            user.first_name = first_name
            user.last_name = last_name
            user.save()
        else:
            user.set_unusable_password()
            user.save()
        return Response({"message": "User registered successfully. Email verification sent."}, status=201)
    except Exception as e:
        logger.exception("register_firebase_email failed")
        if settings.DEBUG:
            return Response({"error": str(e)}, status=500)
        return Response(
            {"error": "Unable to register the provided Firebase email right now. Please try again later."},
            status=500,
        )


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def user_profile(request):
    user = request.user
    if request.method == "GET":
        serializer = UserProfileSerializer(user)
        return Response(serializer.data)
    if request.method == "PUT":
        data = request.data
        user.first_name = data.get("first_name", user.first_name)
        user.last_name = data.get("last_name", user.last_name)
        user.save()
        serializer = UserProfileSerializer(user)
        return Response(serializer.data, status=200)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def resend_verification(request):
    user = request.user
    try:
        _ = firebase_auth.get_user_by_email(user.email)
        link = firebase_auth.generate_email_verification_link(user.email)
        send_mail(
            subject="Verify your email",
            message=f"Click here: {link}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
        return Response({"message": "Verification email sent."}, status=200)
    except Exception as e:
        logger.exception("resend_verification failed")
        if settings.DEBUG:
            return Response({"error": str(e)}, status=400)
        return Response(
            {"error": "Unable to send verification email at this time. Please try again later."},
            status=400,
        )


# -----------------------------------------------------------------------------
# Payments (pricing, PI, Checkout Session, webhook, verify)
# -----------------------------------------------------------------------------
class PaymentScopeThrottle(ScopedRateThrottle):
    scope = "payments"


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PaymentScopeThrottle])
def pricing(request):
    country  = detect_country(request)
    currency = COUNTRY_TO_CURRENCY.get(country, "USD")

    # Baseline table
    base = get_prices_for_currency(currency)

    # Effective prices (plan-by-plan), plus the source ("offer" or "plan")
    effective = {}
    for plan in ("basic", "premium", "pro"):
        price, months, days, source = get_effective_price_and_duration(plan, currency)
        effective[plan] = {
            "amount_minor": price,
            "duration_months": months,
            "duration_days": days,
            "source": source
        }

    return Response({
        "country": country,
        "currency": currency,
        "prices_minor": base,
        "effective": effective
    })


from .pricing import get_effective_price_and_duration

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentScopeThrottle])
def create_payment(request):
    provider = (request.data.get("provider") or "stripe").lower()
    plan_type = (request.data.get("plan_type") or "premium").lower()
    if plan_type not in ("premium", "pro"):
        return Response({"error": "Unsupported plan_type"}, status=400)

    country = detect_country(request)
    currency = COUNTRY_TO_CURRENCY.get(country, "USD")
    try:
        amount_minor = get_price_for_plan(plan_type, currency)
    except ValueError:
        return Response({"error": "Pricing not configured for this plan"}, status=400)
    duration_months = get_plan_duration(plan_type, currency)

    amount_minor, months, days, source = get_effective_price_and_duration(plan_type, currency)

    payment = Payment.objects.create(
        user=request.user,
        provider="razorpay",  # or "stripe"
        plan_type=plan_type,
        amount=amount_minor,
        currency=currency,
        country=country,
        duration_months=months or None,
        duration_days=days or None,   # <-- store days when offer present
        status="created",
    )

    if provider == "stripe":
        if not settings.STRIPE_SECRET_KEY:
            return Response({"error": "Stripe not configured"}, status=500)
        stripe.api_key = settings.STRIPE_SECRET_KEY

        intent = stripe.PaymentIntent.create(
            amount=amount_minor,
            currency=currency.lower(),
            metadata={"payment_id": str(payment.id), "plan_type": plan_type, "user_id": str(request.user.id)},
            description=f"{plan_type.title()} plan",
            automatic_payment_methods={"enabled": True},
        )
        payment.external_id = intent.id
        payment.save(update_fields=["external_id"])
        return Response({"client_secret": intent.client_secret, "payment_id": payment.id})

    if provider in ("razorpay", "paypal"):
        return Response({"message": f"{provider.title()} integration pending", "payment_id": payment.id}, status=202)

    return Response({"error": "Unknown provider"}, status=400)


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    endpoint_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)
    if not endpoint_secret or not sig_header:
        return HttpResponse(status=400)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception:
        return HttpResponse(status=400)

    if event.get("type") == "payment_intent.succeeded":
        stripe.api_key = settings.STRIPE_SECRET_KEY
        obj = (event.get("data") or {}).get("object", {}) or {}
        pi_id = obj.get("id")
        try:
            intent = stripe.PaymentIntent.retrieve(pi_id, expand=["charges.data.billing_details"])
        except Exception:
            intent = obj

        md = intent.get("metadata") or {}
        user_id = md.get("user_id")
        plan = md.get("plan_type")
        payment_id = md.get("payment_id")

        User = get_user_model()
        user = None
        duration_months = 1
        payment_obj = None

        if user_id:
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                user = None

        if payment_id:
            payment_obj = (
                Payment.objects.select_related("user").filter(id=payment_id).first()
            )
        if not payment_obj and pi_id:
            payment_obj = (
                Payment.objects.select_related("user").filter(external_id=pi_id).first()
            )

        if payment_obj:
            duration_months = payment_obj.duration_months or 1
            if not user and payment_obj.user_id:
                user = payment_obj.user
            if not plan:
                plan = payment_obj.plan_type

        if not user and pi_id:
            pmt = Payment.objects.filter(external_id=pi_id).select_related("user").first()
            if pmt and pmt.user_id:
                user = pmt.user
                if not plan:
                    plan = pmt.plan_type
                if not payment_obj:
                    payment_obj = pmt
                    duration_months = pmt.duration_months or 1

        if not user:
            try:
                charges = (intent.get("charges") or {}).get("data", [])
                if charges:
                    email = (charges[-1].get("billing_details") or {}).get("email")
                    if email:
                        user = User.objects.filter(email__iexact=email).first()
            except Exception:
                pass

        if not user:
            return HttpResponse(status=200)  # ack; nothing else we can do

        # Mark payment success (idempotent)
        if payment_obj:
            payment_obj.status = "succeeded"
            if pi_id and not payment_obj.external_id:
                payment_obj.external_id = pi_id
            payment_obj.save(update_fields=["status", "external_id", "updated_at"])
        elif payment_id:
            Payment.objects.filter(id=payment_id).update(status="succeeded", external_id=pi_id)
        else:
            Payment.objects.filter(external_id=pi_id).update(status="succeeded")

        # Apply plan if known
        if plan:
            try:
                apply_subscription(user, plan, months=duration_months)
            except ValueError:
                logger.warning("stripe_webhook: unsupported plan_type=%s", plan)

    return HttpResponse(status=200)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_checkout_session(request):
    """
    Body: { "plan_type": "basic" | "premium" | "pro", "currency": "INR" }
    """
    user = request.user
    plan_type = (request.data.get("plan_type") or "basic").lower()
    currency = request.data.get("currency", "INR")

    duration_months = get_plan_duration(plan_type, currency)
    try:
        amount_minor = get_price_for_plan(plan_type, currency)
    except ValueError:
        return Response({"error": "Pricing not configured for this plan"}, status=400)

    PRICE_LOOKUP = {
        ("basic", "INR"): "price_basic_inr",
        ("premium", "INR"): "price_premium_inr",
        ("pro", "INR"): "price_pro_inr",
        # Add USD/EUR variants if needed
    }

    price_id = PRICE_LOOKUP.get((plan_type, currency))
    if not price_id:
        return Response({"error": "Unknown plan/currency"}, status=400)

    payment = Payment.objects.create(
        user=user,
        provider="stripe",
        plan_type=plan_type,
        amount=amount_minor,
        currency=currency,
        duration_months=duration_months,
        status="created",
    )

    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.FRONTEND_BASE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.FRONTEND_BASE_URL}/billing",
        customer_email=user.email or None,
        metadata={"user_id": str(user.id), "plan_type": plan_type, "payment_id": str(payment.id)},
    )

    payment.external_id = session.id
    payment.save(update_fields=["external_id"])
    return Response({"checkout_url": session.url})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def stripe_confirm_session(request):
    session_id = request.query_params.get("session_id")
    if not session_id:
        return Response({"error": "Missing session_id"}, status=400)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return Response({"error": "Invalid session"}, status=400)

    metadata = session.get("metadata", {}) or {}
    plan = metadata.get("plan_type")
    payment_id = metadata.get("payment_id")

    payment_obj = None
    if payment_id:
        payment_obj = Payment.objects.filter(id=payment_id).first()
    if not payment_obj:
        payment_obj = Payment.objects.filter(external_id=session_id).first()

    if session.get("payment_status") == "paid":
        if payment_obj:
            payment_obj.status = "succeeded"
            payment_obj.external_id = session_id
            payment_obj.save(update_fields=["status", "external_id", "updated_at"])
        elif payment_id:
            Payment.objects.filter(id=payment_id).update(status="succeeded", external_id=session_id)
        else:
            Payment.objects.filter(external_id=session_id).update(status="succeeded")
        resolved_plan = plan
        if not resolved_plan and payment_id:
            resolved_plan = Payment.objects.filter(id=payment_id).values_list("plan_type", flat=True).first()
        if not resolved_plan:
            resolved_plan = Payment.objects.filter(external_id=session_id).values_list("plan_type", flat=True).first()

        if resolved_plan:
            try:
                months = payment_obj.duration_months if payment_obj else 1
                apply_subscription(request.user, resolved_plan, months=months)
            except ValueError:
                logger.warning("stripe_confirm_session: unsupported plan_type=%s", resolved_plan)

    sub = get_active_subscription(request.user)
    return Response(
        {
            "ok": True,
            "plan": sub.plan_type if sub else "basic",
            "upload_limit": sub.limit if sub else None,
            "expires_at": getattr(sub, "expires_at", None),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_payment_intent(request):
    """
    Body: { "plan_type": "basic"|"premium"|"pro", "currency": "INR" }
    Returns: { client_secret }
    """
    user = request.user
    plan_type = (request.data.get("plan_type") or "basic").lower()
    currency = request.data.get("currency", "INR")

    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        amount = get_price_for_plan(plan_type, currency)
    except ValueError:
        return Response({"error": "Pricing not configured for this plan"}, status=400)
    duration_months = get_plan_duration(plan_type, currency)

    payment = Payment.objects.create(
        user=user,
        provider="stripe",
        status="created",
        plan_type=plan_type,
        amount=amount,
        currency=currency,
        duration_months=duration_months,
    )

    intent = stripe.PaymentIntent.create(
        amount=amount,
        currency=currency.lower(),
        automatic_payment_methods={"enabled": True},
        metadata={"user_id": str(user.id), "plan_type": plan_type, "payment_id": str(payment.id)},
    )

    payment.external_id = intent.id
    payment.save(update_fields=["external_id"])
    return Response({"client_secret": intent.client_secret}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])  # safe: we verify with Stripe directly
def verify_intent(request):
    """
    GET /api/payments/verify-intent/?pi=pi_xxx
    Returns 200 and upgrades plan if PI is succeeded (idempotent).
    """
    pi = request.query_params.get("pi")
    if not pi:
        return Response({"ok": False, "error": "missing pi"}, status=400)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        intent = stripe.PaymentIntent.retrieve(pi, expand=["charges.data.billing_details"])
    except Exception:
        return Response({"ok": False, "error": "invalid intent"}, status=400)

    status_str = intent.get("status")
    if status_str not in ("succeeded", "processing"):
        return Response({"ok": False, "error": f"status={status_str}"}, status=400)

    md = intent.get("metadata") or {}
    user_id = md.get("user_id")
    plan = md.get("plan_type")
    payment_id = md.get("payment_id")

    User = get_user_model()
    user = None
    resolved_via = None
    payment_obj = None

    if user_id:
        try:
            user = User.objects.get(id=user_id)
            resolved_via = "metadata.user_id"
        except User.DoesNotExist:
            user = None

    if payment_id:
        payment_obj = (
            Payment.objects.select_related("user").filter(id=payment_id).first()
        )
    if not payment_obj:
        payment_obj = (
            Payment.objects.select_related("user").filter(external_id=pi).first()
        )

    if not user and payment_obj and payment_obj.user_id:
        user = payment_obj.user
        resolved_via = "local Payment.payment_id" if payment_id else "local Payment.external_id"
        if not plan:
            plan = payment_obj.plan_type

    if not user:
        try:
            charges = (intent.get("charges") or {}).get("data", [])
            if charges:
                email = (charges[-1].get("billing_details") or {}).get("email")
                if email:
                    user = User.objects.filter(email__iexact=email).first()
                    if user:
                        resolved_via = "latest_charge.email"
        except Exception:
            pass

    if not user:
        return Response({"ok": False, "error": "user not found"}, status=400)

    if payment_obj:
        payment_obj.status = "succeeded"
        payment_obj.external_id = pi
        payment_obj.save(update_fields=["status", "external_id", "updated_at"])
    elif payment_id:
        Payment.objects.filter(id=payment_id).update(status="succeeded", external_id=pi)
    else:
        Payment.objects.filter(external_id=pi).update(status="succeeded")

    if plan:
        try:
            months = payment_obj.duration_months if payment_obj else 1
            sub = apply_subscription(user, plan, months=months)
        except ValueError:
            logger.warning("verify_intent: unsupported plan_type=%s", plan)
        else:
            return Response({
                "ok": True,
                "plan": sub.plan_type,
                "upload_limit": sub.limit,
                "expires_at": getattr(sub, "expires_at", None),
                "resolved_via": resolved_via,
            }, status=200)
    else:
        sub = get_active_subscription(user)
        return Response(
            {
                "ok": True,
                "plan": getattr(sub, "plan_type", None),
                "upload_limit": sub.limit if sub else None,
                "expires_at": getattr(sub, "expires_at", None),
                "resolved_via": resolved_via,
                "warning": "plan not found in metadata; upgrade applied only if present",
            },
            status=200,
        )


def send_expiry_reminders(days_before=6):
    target_date = now().date() + timedelta(days=days_before)
    qs = SubscriptionPlan.objects.filter(expires_at=target_date)
    sent = 0
    for sub in qs.select_related("user"):
        user = sub.user
        try:
            send_mail(
                subject="Your plan is expiring soon",
                message=(
                    f"Hi {user.username}, your {sub.plan_type.title()} plan expires on {sub.expires_at}. "
                    f"Renew to keep your benefits."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email or user.username],
                fail_silently=True,
            )
            sent += 1
        except Exception:
            pass
    return sent


# -----------------------------------------------------------------------------
# JD extract / generate (Gemini structured output + robust fallbacks)
# -----------------------------------------------------------------------------
class JDExtractThrottle(ScopedRateThrottle):
    scope = "jd_extract"


class JDGenerateThrottle(ScopedRateThrottle):
    scope = "jd_generate"


MAX_UPLOAD_MB = 10
ALLOWED_MIME_PREFIXES = ("image/", "application/pdf")

_CANON = {
    r"\bmern\b": "MERN",
    r"\bmongo\s*db\b|\bmongodb\b": "MongoDB",
    r"\bexpress(\.js)?\b": "Express.js",
    r"\breact(\.js)?\b": "React",
    r"\bnode(\.js)?\b": "Node.js",
    r"\brest\s*api(s)?\b": "REST APIs",
    r"\bgraphql\b": "GraphQL",
    r"\bchat\s*bot(s)?\b|\bchatbot(s)?\b": "Chatbots",
    r"\b(ai|ml|machine learning|artificial intelligence)\b": "AI/ML",
    r"\bdata analysis\b|\banalytics?\b": "Data Analysis",
    r"\bcrm\b": "CRM",
    r"\bhrms\b": "HRMS",
    r"\bhospital management system\b": "Hospital Management System",
    r"\bapi(s)?\b|\bthird[-\s]?party api(s)?\b": "API Integration",
    r"\bauthentication\b|\bauthorization\b|\bjwt\b|\boauth(2)?\b": "Auth/Security",
    r"\bsecurity\b": "Application Security",
    r"\bperformance\b|\bscalab(le|ility)\b|\bresponsive(ness)?\b": "Performance & Responsiveness",
    r"\bui\b|\bux\b|\buser interface\b": "UI/UX",
    r"\btypescript\b": "TypeScript",
    r"\bjavascript\b": "JavaScript",
    r"\bdocker\b": "Docker",
    r"\bkubernetes\b": "Kubernetes",
    r"\bci/?cd\b": "CI/CD",
    r"\btesting\b|\bjest\b|\bplaywright\b|\bcypress\b": "Testing",
}


def _file_too_big(uploaded_file) -> bool:
    return uploaded_file.size > MAX_UPLOAD_MB * 1024 * 1024


def _detect_mime(uploaded_file) -> str:
    mime, _ = mimetypes.guess_type(uploaded_file.name)
    return uploaded_file.content_type or mime or "application/octet-stream"


def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    text_parts: List[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = (page.extract_text() or "").strip()
                if t:
                    text_parts.append(t)
    except Exception as e:
        logger.info(f"pdfplumber failed, will OCR: {e}")

    full_text = "\n".join(text_parts).strip()
    if full_text:
        return full_text

    try:
        images = convert_from_bytes(pdf_bytes, dpi=300, fmt="jpeg", first_page=1)
        ocr_texts = []
        for img in images[:3]:
            ocr_texts.append(pytesseract.image_to_string(img))
        return "\n".join(ocr_texts).strip()
    except Exception as e:
        logger.exception("OCR fallback failed for PDF")
        raise e


def _extract_text_from_image(uploaded_file) -> str:
    try:
        img = Image.open(uploaded_file).convert("RGB")
        return pytesseract.image_to_string(img).strip()
    except Exception as e:
        logger.exception("Image OCR failed")
        raise e


def _extract_text(uploaded_file) -> Tuple[str, str]:
    mime = _detect_mime(uploaded_file)
    if not mime.startswith(ALLOWED_MIME_PREFIXES):
        raise ValueError("Only PDF or image files are supported.")
    if mime == "application/pdf":
        return _extract_text_from_pdf_bytes(uploaded_file.read()), mime
    return _extract_text_from_image(uploaded_file), mime


def _canonicalize(term: str) -> str:
    t = term.strip()
    for pat, rep in _CANON.items():
        if re.search(pat, t, flags=re.I):
            return rep
    return re.sub(r"\s+", " ", t).strip().strip(",.;:").title()


def _fallback_skills_from_jd(jd_text: str, max_n: int = 15) -> List[str]:
    if not jd_text:
        return []
    found: List[str] = []
    lower = jd_text.lower()
    for pat, rep in _CANON.items():
        if re.search(pat, lower, flags=re.I) and rep not in found:
            found.append(rep)
    extra = re.findall(r"\b([A-Z][A-Za-z0-9\.\+#/-]{2,})\b", jd_text)
    for tok in extra:
        c = _canonicalize(tok)
        if c and c not in found and len(c) <= 30:
            found.append(c)
    return found[:max_n]


def _ensure_skills(skills: List[str], jd_text: str) -> List[str]:
    out: List[str] = []
    for s in skills or []:
        cs = _canonicalize(s)
        if cs and cs not in out:
            out.append(cs)
    if len(out) < 8:
        fb = _fallback_skills_from_jd(jd_text)
        for s in fb:
            if s not in out:
                out.append(s)
    return out[:15]


def gemini_structured_generate(jd_text: str) -> dict:
    prompt = f"""
You are a resume optimization assistant.

TASK:
1) Create an ATS-friendly professional summary tailored to the following Job Description (JD).
2) Extract 10–15 relevant skills (ONLY skills list; concise tokens like "React", "Node.js", "REST APIs").
3) Generate 5–8 bullet points for 'Experience' that are action-oriented and measurable.

STRICT OUTPUT AS JSON (no extra text, no code fences):
{{
  "summary": "<string>",
  "skills": ["<skill1>", "<skill2>", "..."],
  "experiences": ["<bullet1>", "<bullet2>", "..."]
}}

RULES:
- 'skills' must be non-empty; prefer concrete technologies, frameworks, or short capability phrases.
- No duplicates. No explanations outside JSON.

JD:
\"\"\"{jd_text[:8000]}\"\"\"
"""
    txt = gemini_client.generate_text([prompt])
    data: dict = {}
    try:
        t = txt.strip()
        if t.startswith("```"):
            t = t.strip("`")
            nl = t.find("\n")
            if nl != -1:
                t = t[nl + 1 :]
        data = json.loads(t)
    except Exception:
        start, end = txt.find("{"), txt.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(txt[start : end + 1])
        else:
            data = {"summary": "", "skills": [], "experiences": []}

    summary = (data.get("summary") or "").strip()
    raw_skills = data.get("skills")
    if isinstance(raw_skills, str):
        raw_skills = [p.strip() for p in raw_skills.split(",") if p.strip()]
    if not isinstance(raw_skills, list):
        raw_skills = []
    skills = _ensure_skills([s for s in raw_skills if isinstance(s, str)], jd_text)

    raw_exp = data.get("experiences")
    if isinstance(raw_exp, str):
        raw_exp = [b.strip("-• ").strip() for b in raw_exp.split("\n") if b.strip()]
    if not isinstance(raw_exp, list):
        raw_exp = []
    experiences = [b.strip() for b in raw_exp if isinstance(b, str) and b.strip()][:8]

    return {"summary": summary, "skills": skills, "experiences": experiences}


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([JDExtractThrottle])
def jd_extract(request):
    try:
        up = request.FILES.get("file")
        if not up:
            return Response({"error": "No file uploaded (field 'file' required)."}, status=400)
        if _file_too_big(up):
            return Response({"error": f"File too large. Max {MAX_UPLOAD_MB} MB."}, status=413)

        text, _ = _extract_text(up)
        if not text:
            return Response({"error": "Could not extract any text from the file."}, status=422)
        return Response({"text": text})
    except ValueError as ve:
        return Response({"error": str(ve)}, status=400)
    except Exception:
        logger.exception("jd_extract failed")
        return Response({"error": "Extraction failed. Please try another file."}, status=500)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([JDGenerateThrottle])
def jd_generate(request):
    try:
        jd_text = request.data.get("jd_text") or request.POST.get("jd_text")
        if not jd_text or not jd_text.strip():
            return Response({"error": "jd_text is required and must not be empty."}, status=400)
        if len(jd_text) > 20000:
            return Response({"error": "jd_text too long (max 20k chars)."}, status=413)

        data = gemini_structured_generate(jd_text)
        data.setdefault("summary", "")
        data.setdefault("skills", [])
        data.setdefault("experiences", [])
        if not isinstance(data["skills"], list):
            data["skills"] = []
        if not isinstance(data["experiences"], list):
            data["experiences"] = []

        return Response(data, status=200)
    except ValueError as ve:
        logger.warning("jd_generate validation error: %s", ve)
        return Response({"error": str(ve)}, status=422)
    except RuntimeError as re_err:
        logger.error("jd_generate configuration error: %s", re_err)
        return Response({"error": "Server configuration error."}, status=500)
    except Exception:
        logger.exception("jd_generate failed")
        return Response({"error": "Generation failed. Please try again."}, status=502)


# -----------------------------------------------------------------------------
# PDF Generation (WeasyPrint) with sanitize/markdown helpers
# -----------------------------------------------------------------------------
ALLOWED_TAGS = ["p", "br", "strong", "b", "em", "i", "u", "s", "del", "a", "ul", "ol", "li", "span"]
ALLOWED_ATTRS = {"a": ["href", "title", "target", "rel"], "span": ["style"]}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

import bleach  # after constants to avoid clutter


def _looks_like_html(text: str) -> bool:
    return "<" in (text or "") and ">" in (text or "")


def _preprocess_underline(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"__(.+?)__", r"<u>\1</u>", text)


def _markdown_to_html(text: str) -> str:
    pre = _preprocess_underline(text)
    return md(pre, extensions=["extra", "sane_lists", "nl2br", "pymdownx.tilde"], output_format="xhtml1")


def _sanitize_html(html: str) -> str:
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, protocols=ALLOWED_PROTOCOLS, strip=True)
    return bleach.linkify(cleaned, callbacks=[bleach.callbacks.nofollow])


def _rich(text: str) -> str:
    if not text:
        return ""
    if _looks_like_html(text):
        return _sanitize_html(text)
    return _sanitize_html(_markdown_to_html(text))


def _normalize_payload(resume: dict) -> dict:
    for e in resume.get("experience", []):
        if e.get("isCurrentJob") and not (e.get("endDate") or "").strip():
            e["endDate"] = None
        for k in ("jobTitle", "company", "location", "startDate", "endDate", "description"):
            if k in e and isinstance(e[k], str):
                e[k] = e[k].strip()

    for ed in resume.get("education", []):
        if ed.get("isCurrentlyStudying") and not (ed.get("endDate") or "").strip():
            ed["endDate"] = None
        for k in ("institution", "degree", "fieldOfStudy", "startDate", "endDate", "description", "gpa"):
            if k in ed and isinstance(ed[k], str):
                ed[k] = ed[k].strip()

    for p in resume.get("projects", []):
        if p.get("isOngoing") and not (p.get("endDate") or "").strip():
            p["endDate"] = None
        for k in ("name", "description", "startDate", "endDate", "url", "githubUrl"):
            if k in p and isinstance(p[k], str):
                p[k] = p[k].strip()
        if isinstance(p.get("technologies"), list):
            p["technologies"] = [t.strip() for t in p["technologies"] if str(t).strip()]

    contacts = resume.get("contacts", {})
    for k in ("firstName", "lastName", "email", "phone", "location", "website", "linkedin", "github"):
        if k in contacts and isinstance(contacts[k], str):
            contacts[k] = contacts[k].strip()
    resume["contacts"] = contacts

    if isinstance(resume.get("summary"), str):
        resume["summary"] = resume["summary"].strip()

    for s in resume.get("skills", []):
        for k in ("name", "category", "level"):
            if k in s and isinstance(s[k], str):
                s[k] = s[k].strip()

    for c in resume.get("certificates", []):
        for k in ("name", "issuer", "issueDate", "expirationDate", "credentialId", "url"):
            if k in c and isinstance(k, str):
                c[k] = c[k].strip()

    return resume


def _apply_rich_to_payload(resume: dict) -> dict:
    resume["summary_html"] = _rich(resume.get("summary", ""))
    for e in resume.get("experience", []):
        e["description_html"] = _rich(e.get("description", ""))
    for ed in resume.get("education", []):
        ed["description_html"] = _rich(ed.get("description", ""))
    for p in resume.get("projects", []):
        p["description_html"] = _rich(p.get("description", ""))
    return resume


class GenerateResumePDFView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = GeneratePayloadSerializer(data=request.data)
        if not ser.is_valid():
            logger.debug("PDF serializer errors: %s", ser.errors)
            return Response(ser.errors, status=400)

        template_key = ser.validated_data["template"]
        resume_format = ser.validated_data["format"]  # with-photo|without-photo
        resume = _normalize_payload(ser.validated_data["data"])
        resume = _apply_rich_to_payload(resume)

        skills_names = [s["name"] for s in resume.get("skills", [])]
        full_name = f"{resume['contacts']['firstName']} {resume['contacts']['lastName']}".strip()

        context = {
            "contacts": resume["contacts"],
            "summary_html": resume.get("summary_html", ""),
            "experience": resume.get("experience", []),
            "education": resume.get("education", []),
            "certificates": resume.get("certificates", []),
            "projects": resume.get("projects", []),
            "skills": skills_names,
            "photo": resume.get("photo") if resume_format == "with-photo" else None,
            "full_name": full_name,
        }

        try:
            html_string = render_to_string(f"resume/{template_key}.html", context)
            css_path = finders.find("css/pdf_base.css")
            stylesheets = [CSS(filename=css_path)] if css_path else []
            base_url = request.build_absolute_uri("/")
            pdf_bytes = HTML(string=html_string, base_url=base_url).write_pdf(stylesheets=stylesheets)

            import datetime

            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{(full_name or 'resume').replace(' ', '-')}_{template_key}_{stamp}.pdf"

            resp = HttpResponse(pdf_bytes, content_type="application/pdf")
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp
        except Exception as e:
            logger.exception("PDF generation failed")
            if settings.DEBUG:
                return Response({"detail": "Failed to generate PDF", "error": str(e)}, status=500)
            return Response(
                {
                    "detail": "Failed to generate PDF",
                    "error": "Something went wrong while processing your request. Please try again later.",
                },
                status=500,
            )


# -----------------------------------------------------------------------------
# AI Suggest (Gemini) – protected
# -----------------------------------------------------------------------------
from .ai.gemini_service import generate_resume_points  # unchanged import

class GeminiSuggestView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "resume"

    def post(self, request):
        prompt = (request.data or {}).get("prompt", "")
        try:
            text = generate_resume_points(prompt)
            return Response({"result": text})
        except ValueError as ve:
            return Response({"detail": str(ve)}, status=400)
        except Exception:
            return Response({"detail": "AI service error"}, status=502)



# checker_app/views.py (add imports)
import hmac, hashlib
import razorpay
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.http import HttpResponse

from .pricing import get_effective_price_and_duration


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentScopeThrottle])
def razorpay_create_order(request):
    """
    Body: { "plan_type": "premium"|"pro" }
    Returns: { key_id, order, payment_id, plan_type }
    """
    user = request.user
    plan_type = (request.data.get("plan_type") or "premium").lower()
    if plan_type not in ("premium", "pro"):
        return Response({"error": "Unsupported plan_type"}, status=400)

    # Resolve geo -> currency as you already do everywhere else:
    country = detect_country(request)
    currency = COUNTRY_TO_CURRENCY.get(country, "USD")

    # 👇 **Use the central pricing helpers**
    try:
        amount_minor = get_price_for_plan(plan_type, currency)
    except ValueError:
        return Response({"error": "Pricing not configured for this plan"}, status=400)

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return Response({"error": "Razorpay not configured"}, status=500)

    # Local Payment row
    amount_minor, months, days, source = get_effective_price_and_duration(plan_type, currency)

    duration_months = int(months or 0)
    duration_days   = int(days or 0)

    payment = Payment.objects.create(
        user=request.user,
        provider="razorpay",  # or "stripe"
        plan_type=plan_type,
        amount=amount_minor,
        currency=currency,
        country=country,
        duration_months=duration_months,
        duration_days=(duration_days or None),   # <-- store days when offer present
        status="created",
        # keep whatever else you already set
    )

    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    order = client.order.create({
        "amount": amount_minor,                 # paise/cents
        "currency": currency,
        "receipt": f"pay_{payment.id}",
        "payment_capture": 1,
        "notes": {"user_id": str(user.id), "plan_type": plan_type, "payment_id": str(payment.id)},
    })

    payment.external_id = order.get("id") or ""
    payment.save(update_fields=["external_id", "updated_at"])

    return Response({
        "key_id": settings.RAZORPAY_KEY_ID,
        "order": order,
        "payment_id": payment.id,
        "plan_type": plan_type,
    }, status=200)


# ---------- Razorpay: verify signature from frontend ----------
@api_view(["POST"])
@permission_classes([IsAuthenticated])  # user verifies immediately after success
@throttle_classes([PaymentScopeThrottle])
def razorpay_verify(request):
    """
    Body: { "razorpay_order_id", "razorpay_payment_id", "razorpay_signature", "payment_id" }
    Verifies HMAC and upgrades subscription idempotently.
    """
    required = ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature", "payment_id")
    if not all(k in request.data for k in required):
        return Response({"ok": False, "error": "missing fields"}, status=400)

    order_id = request.data["razorpay_order_id"]
    rp_payment_id = request.data["razorpay_payment_id"]
    rp_signature = request.data["razorpay_signature"]
    local_payment_id = request.data["payment_id"]

    # Step 1: verify HMAC
    body = f"{order_id}|{rp_payment_id}".encode("utf-8")
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, rp_signature):
        # mark failed and bail
        Payment.objects.filter(id=local_payment_id).update(status="failed")
        return Response({"ok": False, "error": "signature mismatch"}, status=400)

    # Step 2: mark success & upgrade
    pay = Payment.objects.filter(id=local_payment_id, provider="razorpay").first()
    if not pay:
        return Response({"ok": False, "error": "payment not found"}, status=404)

    pay.status = "succeeded"
    # store gateway ids if you added fields
    # pay.razorpay_payment_id = rp_payment_id
    # pay.razorpay_signature = rp_signature
    pay.save(update_fields=["status", "updated_at"])

    # fallback to plan + duration snapshot from our Payment row
    plan = pay.plan_type
    try:
        months = pay.duration_months or 0
        days   = pay.duration_days or 0
        if (months or 0) <= 0 and (days or 0) <= 0:
            months = 1
        sub = apply_subscription(request.user, plan, months=months, days=days)

    except ValueError:
        # plan mismatch or unsupported
        sub = get_active_subscription(request.user)

    return Response({
        "ok": True,
        "plan": getattr(sub, "plan_type", plan),
        "upload_limit": getattr(sub, "limit", None),
        "expires_at": getattr(sub, "expires_at", None),
    }, status=200)


# ---------- Razorpay: webhook ----------
@csrf_exempt
def razorpay_webhook(request):
    """
    Configure in Razorpay Dashboard with RAZORPAY_WEBHOOK_SECRET.
    Handles payment.captured/payment.failed/order.paid (idempotent).
    """
    try:
        signature = request.META.get("HTTP_X_RAZORPAY_SIGNATURE") or ""
        payload = request.body
        secret = settings.RAZORPAY_WEBHOOK_SECRET
        if not secret or not signature:
            return HttpResponse(status=400)

        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return HttpResponse(status=400)

        event = json.loads(payload.decode("utf-8"))
        etype = event.get("event")
        data = (event.get("payload") or {})
        # Depending on type, entity may be 'payment' or 'order'
        payment_entity = (data.get("payment") or {}).get("entity") or {}
        order_entity = (data.get("order") or {}).get("entity") or {}
        order_id = order_entity.get("id") or payment_entity.get("order_id")
        rp_payment_id = payment_entity.get("id")

        # Resolve our Payment row
        qs = Payment.objects.filter(provider="razorpay", external_id=order_id)
        pmt = qs.select_related("user").first()

        if etype in ("payment.captured", "order.paid"):
            # mark success
            qs.update(status="succeeded")
            if pmt and pmt.user_id:
                try:
                    months = (pmt.duration_months or 0)
                    days = (pmt.duration_days or 0)
                    apply_subscription(pmt.user, pmt.plan_type, months=months, days=days)
                except ValueError:
                    logger.warning("razorpay_webhook: unsupported plan=%s", pmt.plan_type)
        elif etype in ("payment.failed",):
            qs.update(status="failed")

        return HttpResponse(status=200)
    except Exception:
        logger.exception("razorpay_webhook failed")
        return HttpResponse(status=200)  # ack to avoid retries storm
