"""Helper utilities to ensure Firebase Admin SDK is ready before use."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

import firebase_admin
from django.conf import settings
from firebase_admin import credentials

logger = logging.getLogger(__name__)

_init_lock = threading.Lock()


class FirebaseInitError(RuntimeError):
    """Raised when the Firebase Admin SDK cannot be initialized."""


def _as_path(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.exists() else None


def _candidate_credential_sources() -> list[Path]:
    base_dir = Path(getattr(settings, "BASE_DIR", Path.cwd()))

    paths = [
        _as_path(os.getenv("FIREBASE_SERVICE_ACCOUNT")),
        _as_path(getattr(settings, "FIREBASE_CRED_PATH", None)),
        _as_path(base_dir / "firebase_service_account.json"),
        _as_path(base_dir / "quotientone_ats_firebase.json"),
    ]

    seen: set[Path] = set()
    candidates: list[Path] = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            candidates.append(path)
    return candidates


def _load_certificate() -> Optional[credentials.Certificate]:
    # Highest precedence: explicit JSON payload
    firebase_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if firebase_json:
        try:
            data = json.loads(firebase_json)
            return credentials.Certificate(data)
        except Exception as exc:  # json errors or certificate issues
            logger.error("Invalid FIREBASE_SERVICE_ACCOUNT_JSON configuration: %s", exc)

    # Next: check filesystem paths in order of precedence
    for path in _candidate_credential_sources():
        try:
            return credentials.Certificate(path)
        except Exception as exc:
            logger.warning("Failed to load Firebase credentials from %s: %s", path, exc)
    return None


def ensure_firebase_initialized() -> firebase_admin.App:
    """Initialize Firebase Admin SDK exactly once in a thread-safe manner."""
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    with _init_lock:
        try:
            return firebase_admin.get_app()
        except ValueError:
            cred = _load_certificate()
            if cred:
                return firebase_admin.initialize_app(cred)

            # Fall back to Application Default credentials, if available
            try:
                default_cred = credentials.ApplicationDefault()
                return firebase_admin.initialize_app(default_cred)
            except Exception as exc:
                logger.error("Firebase Admin initialization failed: %s", exc)
                raise FirebaseInitError(
                    "Firebase credentials are not configured. Set FIREBASE_SERVICE_ACCOUNT "
                    "or FIREBASE_SERVICE_ACCOUNT_JSON."
                ) from exc

