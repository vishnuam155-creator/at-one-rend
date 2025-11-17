# settings.py (production-safe, Render + Firebase)
from pathlib import Path
import os
import importlib
import json

import dj_database_url
from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------------------
# Core paths & flags
# ------------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "CHANGE-ME-IN-ENV")
# Correct: true means DEBUG mode
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"

# Toggle auth style (default: token/bearer in SPA)
USE_SESSION_AUTH = os.getenv("USE_SESSION_AUTH", "false").lower() == "true"

# ------------------------------------------------------------------------------
# Hosts, CSRF, CORS
# ------------------------------------------------------------------------------
# Keep tight in prod; extend via env if needed
DEFAULT_ALLOWED_HOSTS = [
    "a-t-s-resume-backend-api.onrender.com",
    "localhost",
    "127.0.0.1",
]
ALLOWED_HOSTS = [u.strip() for u in os.getenv("DJANGO_ALLOWED_HOSTS", ",".join(DEFAULT_ALLOWED_HOSTS)).split(",") if u.strip()]

DEFAULT_FRONTEND_ORIGINS = [
    "https://quotientone.cloud",
    "https://www.quotientone.cloud",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
CORS_ALLOWED_ORIGINS = [
    u.strip()
    for u in os.getenv(
        "DJANGO_CORS_ALLOWED_ORIGINS",
        ",".join(DEFAULT_FRONTEND_ORIGINS),
    ).split(",")
    if u.strip()
]

# CSRF trusted origins should include backend & frontends when using session auth
DEFAULT_CSRF_TRUSTED = [
    "https://quotientone.cloud",
    "https://www.quotientone.cloud",
    "https://a-t-s-resume-backend-api.onrender.com",
]
CSRF_TRUSTED_ORIGINS = [u.strip() for u in os.getenv(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    ",".join(DEFAULT_CSRF_TRUSTED),
).split(",") if u.strip()]

# Session vs Token mode
_allow_cookie_credentials = os.getenv("DJANGO_ALLOW_COOKIE_CREDENTIALS", "true").lower() == "true"
if USE_SESSION_AUTH or _allow_cookie_credentials:
    # Allow cross-site cookies so we can track anonymous usage counters
    CORS_ALLOW_CREDENTIALS = True
    SESSION_COOKIE_SAMESITE = "None"
    CSRF_COOKIE_SAMESITE = "None"

    _secure_default = "false" if DEBUG else "true"
    SESSION_COOKIE_SECURE = os.getenv("DJANGO_SESSION_COOKIE_SECURE", _secure_default).lower() == "true"
    CSRF_COOKIE_SECURE = os.getenv("DJANGO_CSRF_COOKIE_SECURE", _secure_default).lower() == "true"
else:
    # Token/Bearer auth without cookies
    CORS_ALLOW_CREDENTIALS = False

# ------------------------------------------------------------------------------
# Installed apps
# ------------------------------------------------------------------------------
INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",

    # Local
    "checker_app",

    # Third-party
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "drf_spectacular",
    "feedBack",
]

SITE_ID = int(os.getenv("DJANGO_SITE_ID", "1"))

# ------------------------------------------------------------------------------
# Middleware
# ------------------------------------------------------------------------------
MIDDLEWARE = [
    "core.middleware.secure_errors.SecureErrorMiddleware",  # your custom error masker
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # add right after SecurityMiddleware
]

# ------------------------------------------------------------------------------
# URLs / WSGI
# ------------------------------------------------------------------------------
ROOT_URLCONF = "ats_resume_web.urls"
WSGI_APPLICATION = "ats_resume_web.wsgi.application"

# ------------------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ------------------------------------------------------------------------------
# Database (PostgreSQL via DATABASE_URL; SQLite fallback for local dev)
# ------------------------------------------------------------------------------
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "60")),
        ssl_require=os.getenv("DB_SSL_REQUIRE", "true").lower() == "true",
    )
}

# ------------------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
)

ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_EMAIL_VERIFICATION = os.getenv("ACCOUNT_EMAIL_VERIFICATION", "none")

# After login/logout, return user to your Firebase frontend
LOGIN_REDIRECT_URL = os.getenv("LOGIN_REDIRECT_URL", "https://quotientone.cloud/")
LOGOUT_REDIRECT_URL = os.getenv("LOGOUT_REDIRECT_URL", "https://quotientone.cloud/")

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }
}

# ------------------------------------------------------------------------------
# Password validation
# ------------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ------------------------------------------------------------------------------
# I18N / TZ
# ------------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------------------
# Static & Media
# ------------------------------------------------------------------------------
# STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "static_root"
STATICFILES_DIRS = [BASE_DIR / "resume" / "static"] if (BASE_DIR / "resume" / "static").exists() else []

# MEDIA_URL = "media/"
# MEDIA_ROOT = BASE_DIR / "media"

# ------------------------------------------------------------------------------
# Email
# ------------------------------------------------------------------------------
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@example.com")

# ------------------------------------------------------------------------------
# Payments / 3rd-party keys
# ------------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# ------------------------------------------------------------------------------
# Security (prod)
# ------------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# ------------------------------------------------------------------------------
# Firebase (optional)
# ------------------------------------------------------------------------------
try:
    import firebase_admin  # type: ignore
    from firebase_admin import credentials  # type: ignore

    FIREBASE_CRED_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT", str(BASE_DIR / "quotientone_ats_firebase.json"))

    if not firebase_admin._apps:
        if os.path.exists(FIREBASE_CRED_PATH):
            cred = credentials.Certificate(FIREBASE_CRED_PATH)
        else:
            firebase_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
            if firebase_json:
                cred_dict = json.loads(firebase_json)
                cred = credentials.Certificate(cred_dict)
            else:
                raise ValueError("No Firebase credentials found")
        firebase_admin.initialize_app(cred)

except Exception as e:
    print(f"⚠️ Firebase init skipped: {e}")

# ------------------------------------------------------------------------------
# DRF (single, merged, no duplicates)
# ------------------------------------------------------------------------------
_throttle_rates = {
    "anon": os.getenv("DRF_THROTTLE_ANON", "50/min"),
    "user": os.getenv("DRF_THROTTLE_USER", "200/min"),
    "payments": os.getenv("DRF_THROTTLE_PAYMENTS", "20/min"),
    "resume": os.getenv("DRF_THROTTLE_RESUME", "10/min"),
    "jd_extract": os.getenv("DRF_THROTTLE_JD_EXTRACT", "30/hour"),
    "jd_generate": os.getenv("DRF_THROTTLE_JD_GENERATE", "60/hour"),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # Token/Bearer for SPA (works in both modes)
        "rest_framework.authentication.TokenAuthentication",
        # Optionally add SessionAuth only when using session cookies
        *(("rest_framework.authentication.SessionAuthentication",) if USE_SESSION_AUTH else ()),
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": _throttle_rates,
    "EXCEPTION_HANDLER": "core.drf_exception_handler.secure_exception_handler",
}

# If you send Authorization headers, ensure they pass CORS
try:
    from corsheaders.defaults import default_headers
    CORS_ALLOW_HEADERS = list(default_headers) + ["authorization"]
except Exception:
    pass

# ------------------------------------------------------------------------------
# Logging (json if available; fallback to verbose)
# ------------------------------------------------------------------------------
LOG_LEVEL = os.getenv("DJANGO_LOG_LEVEL", "INFO")

_json_logger_available = True
try:
    importlib.import_module("pythonjsonlogger.jsonlogger")  # type: ignore
except ModuleNotFoundError:
    _json_logger_available = False

if _json_logger_available:
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
            }
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "json", "level": LOG_LEVEL},
        },
        "root": {"handlers": ["console"], "level": LOG_LEVEL},
    }
else:
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "verbose", "level": LOG_LEVEL},
            "error_file": {"class": "logging.FileHandler", "filename": BASE_DIR / "error.log", "formatter": "verbose", "level": "ERROR"},
        },
        "root": {"handlers": ["console", "error_file"], "level": LOG_LEVEL},
    }


# STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'


# settings.py (relevant parts)
INSTALLED_APPS += ['storages']

AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')          # or leave out if using instance role
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_STORAGE_BUCKET_NAME = os.getenv('AWS_STORAGE_BUCKET_NAME')
AWS_S3_REGION_NAME = os.getenv('AWS_S3_REGION_NAME', default='us-east-1')
AWS_S3_SIGNATURE_VERSION = 's3v4'  # recommended

# Public/static files
STATICFILES_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
STATIC_URL = f'https://{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com/static/'

# User uploaded media (optional separate storage class)
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
MEDIA_URL = f'https://{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com/media/'