import os, pytest
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ats_resume_web.settings")
@pytest.fixture(scope="session", autouse=True)
def _configure_settings():
    from django.conf import settings
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    return settings