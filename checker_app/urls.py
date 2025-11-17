from django.urls import path, include
from .views import (
    sync_user, get_user_plan, resume_checker, firebase_login, firebase_email_login,
    register_firebase_email, user_profile, resend_verification,
    pricing, create_payment, stripe_webhook ,create_checkout_session, stripe_webhook, stripe_confirm_session,create_payment_intent,verify_intent,GenerateResumePDFView,
    pricing,
    razorpay_create_order,
    razorpay_verify,
    razorpay_webhook,
)


from . import views

urlpatterns = [
    path("api/users/sync/", sync_user),
    path("api/get-user-plan/", get_user_plan, name="get_user_plan"),
    path("resume_checker/", resume_checker, name="resume_checker"),
    path('auth/firebase/', firebase_login, name='firebase_google_login'),
    path('auth/firebase_email_login/', firebase_email_login, name='firebase_email_login'),
    path('auth/register_firebase_email/', register_firebase_email, name='register_firebase_email'),
    path("api/profile/", user_profile, name="user-profile"),
    path("api/resend-verification/", resend_verification, name="resend-verification"),
    path("api/pricing/", pricing, name="pricing"),
    path("api/payments/create/", create_payment, name="create_payment"),
    # path("api/payments/stripe/webhook/", stripe_webhook, name="stripe_webhook"),

    # ====================== create resume JD==========================
    path("jd/extract/", views.jd_extract, name="jd_extract"),
    path("jd/generate/", views.jd_generate, name="jd_generate"),
    # path("resume/checker/", views.resume_checker, name="resume_checker"),  # your existing


    # path("payments/stripe/create-checkout-session/", create_checkout_session),
    # # path("payments/stripe/webhook/", stripe_webhook),          # POST from Stripe
    # path("payments/stripe/confirm/", stripe_confirm_session),  # GET from frontend success page
    # path("api/payments/create/", create_payment_intent),
    # path("api/payments/verify-intent/", verify_intent),
    # path("api/payments/stripe/webhook/", stripe_webhook),

    # resume
    path("api/resume/generate-pdf/",  GenerateResumePDFView.as_view(), name="generate_resume_pdf"),


    path("api/pricing/", pricing),  # you already have this
    path("api/payments/razorpay/create-order/", razorpay_create_order),
    path("api/payments/razorpay/verify/", razorpay_verify),
    path("api/payments/razorpay/webhook/", razorpay_webhook),

]
# 