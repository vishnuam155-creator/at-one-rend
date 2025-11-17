"""Utilities for interacting with the Gemini generative model.

This module focuses on improving inference performance while preserving the
existing behaviour exposed by ``generate_resume_points``.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections import OrderedDict
from functools import lru_cache
from threading import RLock
from typing import Optional

import google.generativeai as genai

_GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

_LOGGER = logging.getLogger(__name__)
_CACHE_LOCK = RLock()
_RESPONSE_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CACHE_SIZE = max(0, int(os.getenv("GEMINI_RESPONSE_CACHE_SIZE", "32")))


def _get_cached_response(prompt: str) -> Optional[str]:
    """Return a cached response for ``prompt`` if available."""

    if _CACHE_SIZE <= 0:
        return None
    with _CACHE_LOCK:
        try:
            cached_value = _RESPONSE_CACHE[prompt]
        except KeyError:
            return None
        _RESPONSE_CACHE.move_to_end(prompt)
        _LOGGER.debug("Gemini response cache hit (len=%d)", len(prompt))
        return cached_value


def _store_cached_response(prompt: str, response: str) -> None:
    """Store a response for ``prompt`` in the LRU cache."""

    if _CACHE_SIZE <= 0:
        return
    with _CACHE_LOCK:
        if prompt in _RESPONSE_CACHE:
            _RESPONSE_CACHE.move_to_end(prompt)
        _RESPONSE_CACHE[prompt] = response
        while len(_RESPONSE_CACHE) > _CACHE_SIZE:
            _RESPONSE_CACHE.popitem(last=False)
        _LOGGER.debug("Gemini response cached (size=%d)", len(_RESPONSE_CACHE))


@lru_cache(maxsize=1)
def _client():
    """Return a configured Gemini client instance."""

    if not _GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY not configured")
    genai.configure(api_key=_GOOGLE_API_KEY)
    return genai.GenerativeModel("gemini-2.5-flash")  # or gemini-1.5-pro


def generate_resume_points(prompt: str, retries: int = 3) -> str:
    """Generate resume bullet points for the supplied ``prompt``."""

    if not prompt or len(prompt) < 5:
        raise ValueError("Prompt is required and must be meaningful")

    cached_response = _get_cached_response(prompt)
    if cached_response is not None:
        return cached_response

    delay, last_error = 1.0, None
    for _ in range(retries):
        try:
            model = _client()
            start_time = time.perf_counter()
            resp = model.generate_content(prompt)
            duration = time.perf_counter() - start_time
            _LOGGER.debug("Gemini inference latency: %.3fs", duration)
            if hasattr(resp, "text") and resp.text:
                cleaned = resp.text.strip()
                _store_cached_response(prompt, cleaned)
                return cleaned
            _store_cached_response(prompt, "")
            return ""
        except Exception as exc:  # pragma: no cover - best effort logging
            last_error = exc
            _LOGGER.exception("Gemini inference failed, retrying")
            time.sleep(delay + random.random())
            delay *= 2

    if last_error is not None:
        raise last_error
    raise RuntimeError("Gemini generation failed without specific error")