"""Every Gemini API call (SPEC 6). Backend only; never send personal data to Gemini.

OWNER: engine team.

What this module does
---------------------
Three calls, each sending only photos and a fixed prompt (never names,
phone numbers, e-mail addresses or locations):

* :meth:`GeminiService.precheck`            - SPEC 6.2: exactly one dog/cat? real photo? quality?
* :meth:`GeminiService.extract_attributes`  - SPEC 6.3: breed guess, colours, pattern, marks ...
* :meth:`GeminiService.compare`             - SPEC 6.5: visual similarity of two photos, 0-100

Every call uses the official ``google-genai`` SDK (checked against the
installed version 2.25.0) with *structured output*
(``response_mime_type="application/json"`` + ``response_schema``), a low
temperature (``GEMINI_TEMPERATURE``), a per-call timeout
(``GEMINI_TIMEOUT_SECONDS``) and up to ``GEMINI_MAX_ATTEMPTS`` attempts with
exponential backoff on HTTP 408/429/5xx, network timeouts and unreadable
answers. The SDK's own retry is left off (its default) so the rule lives here.

Every answer is validated and clamped (``normalize_*`` functions below) so
the rest of the app can trust the shapes even if the model misbehaves.

Thread safety: all settings are read from ``current_app.config`` in
``__init__``; the methods never touch Flask or the database, so the matching
service can call :meth:`compare` from worker threads.

Tests never call the real API: the ``fake_gemini`` fixture in
tests/conftest.py replaces ``precheck``, ``extract_attributes``, ``compare``
and ``is_configured`` on this class, and tests/test_gemini.py drives the real
methods with a fake SDK client.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from flask import current_app, has_app_context
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

log = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_MODEL = "gemini-2.5-flash"

SPECIES_VALUES = ("dog", "cat", "other", "none")
QUALITY_VALUES = ("good", "fair", "poor")
CONFIDENCE_VALUES = ("low", "medium", "high")
SIZE_VALUES = ("small", "medium", "large", "unknown")
COAT_LENGTH_VALUES = ("short", "medium", "long", "hairless", "unknown")

MAX_LIST_ITEMS = 8
MAX_TEXT_LENGTH = 120


class GeminiError(Exception):
    """A Gemini call failed (after retries) or returned unusable output."""


class GeminiNotConfigured(GeminiError):
    """GEMINI_API_KEY is not set; callers fall back to coarse ranking."""


# --- Prompts (SPEC 6.2, 6.3, 6.5) -----------------------------------------------------------

PRECHECK_PROMPT = """You check photos uploaded to a lost-and-found pet service for dogs and cats.
Look at the image and report:
- contains_animal: true if any animal is visible.
- species: "dog" or "cat" if the main animal is a domestic dog or cat, "other" for any
  other animal, "none" if there is no animal.
- animal_count: how many animals are clearly visible.
- image_quality: "good", "fair" or "poor" for identifying the animal (sharpness, lighting,
  how much of the body and face is visible).
- quality_issues: short phrases such as "blurry", "too dark", "face not visible",
  "animal too small", "partly cropped". Empty list if none.
- is_real_photo: false for drawings, cartoons, toys, statues, screenshots of text or
  AI-looking renders; true for a real camera photo of a real animal.
Describe only what is visible. Do not identify or describe any people.
Return only JSON."""

ATTRIBUTES_PROMPT = """Describe the visible physical traits of the single dog or cat in this photo
for a lost-and-found pet service in the Philippines.
- species: "dog" or "cat".
- breed_guess: the most likely breed or "Aspin (mixed)" / "Puspin (mixed)" for local mixed
  breeds. It is only a guess.
- primary_colors: 1 to 3 main coat colours as simple lower-case words (e.g. "black",
  "white", "brown", "tan", "gray", "orange", "cream").
- coat_pattern: one of "solid", "bicolor", "tricolor", "tabby", "calico", "tortoiseshell",
  "brindle", "spotted", "merle", "pointed", "other".
- coat_length: "short", "medium", "long", "hairless" or "unknown".
- size_estimate: "small", "medium", "large" or "unknown" (adult-size estimate).
- distinctive_marks: visible stable marks (patches, spots, scars, eye colour, ear or tail
  shape). Empty list if none.
- accessories: collars, tags, clothes or harnesses. Empty list if none.
Only describe the animal. Do not describe people, places or any text in the image.
Return only JSON."""

COMPARE_PROMPT = """You help people reunite lost dogs and cats with their owners. Compare the
animal in IMAGE A with the animal in IMAGE B.
Compare only stable physical traits: species, body shape and size, coat colour, coat
pattern and length, distinctive markings, ear shape, tail shape, eye colour and facial
structure. Ignore background, lighting, camera angle, pose, image quality and accessories
that can change (collars, clothes) unless they are clearly identical.
- visual_similarity: an integer from 0 to 100 for how visually similar the two animals
  look (100 = practically indistinguishable, 0 = clearly different animals or species).
- matching_features: short phrases for traits that look alike.
- differences: short phrases for traits that look different.
- confidence: "low", "medium" or "high" for how reliable this comparison is.
- comparison_possible: false if either image does not show a dog or cat clearly enough
  to compare.
Do NOT claim that the two animals are the same individual or that anyone owns them; you
only describe visual similarity. Do not describe people.
Return only JSON."""


def _string_list_schema(description: str) -> types.Schema:
    return types.Schema(
        type=types.Type.ARRAY,
        description=description,
        items=types.Schema(type=types.Type.STRING),
        max_items=MAX_LIST_ITEMS,
    )


PRECHECK_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "contains_animal": types.Schema(type=types.Type.BOOLEAN),
        "species": types.Schema(type=types.Type.STRING, enum=list(SPECIES_VALUES)),
        "animal_count": types.Schema(type=types.Type.INTEGER, minimum=0),
        "image_quality": types.Schema(type=types.Type.STRING, enum=list(QUALITY_VALUES)),
        "quality_issues": _string_list_schema("Short phrases describing quality problems."),
        "is_real_photo": types.Schema(type=types.Type.BOOLEAN),
    },
    required=[
        "contains_animal",
        "species",
        "animal_count",
        "image_quality",
        "quality_issues",
        "is_real_photo",
    ],
    property_ordering=[
        "contains_animal",
        "species",
        "animal_count",
        "image_quality",
        "quality_issues",
        "is_real_photo",
    ],
)

ATTRIBUTES_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "species": types.Schema(type=types.Type.STRING, enum=["dog", "cat"]),
        "breed_guess": types.Schema(type=types.Type.STRING),
        "primary_colors": _string_list_schema("1-3 main coat colours, lower case."),
        "coat_pattern": types.Schema(type=types.Type.STRING),
        "coat_length": types.Schema(type=types.Type.STRING, enum=list(COAT_LENGTH_VALUES)),
        "size_estimate": types.Schema(type=types.Type.STRING, enum=list(SIZE_VALUES)),
        "distinctive_marks": _string_list_schema("Visible stable markings."),
        "accessories": _string_list_schema("Collars, tags, clothes, harnesses."),
    },
    required=[
        "species",
        "breed_guess",
        "primary_colors",
        "coat_pattern",
        "coat_length",
        "size_estimate",
        "distinctive_marks",
        "accessories",
    ],
    property_ordering=[
        "species",
        "breed_guess",
        "primary_colors",
        "coat_pattern",
        "coat_length",
        "size_estimate",
        "distinctive_marks",
        "accessories",
    ],
)

COMPARE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "visual_similarity": types.Schema(type=types.Type.INTEGER, minimum=0, maximum=100),
        "matching_features": _string_list_schema("Traits that look alike."),
        "differences": _string_list_schema("Traits that look different."),
        "confidence": types.Schema(type=types.Type.STRING, enum=list(CONFIDENCE_VALUES)),
        "comparison_possible": types.Schema(type=types.Type.BOOLEAN),
    },
    required=[
        "visual_similarity",
        "matching_features",
        "differences",
        "confidence",
        "comparison_possible",
    ],
    property_ordering=[
        "comparison_possible",
        "matching_features",
        "differences",
        "visual_similarity",
        "confidence",
    ],
)


# --- Validation and clamping (pure functions; unit-tested) -------------------------------------


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    if isinstance(value, int | float):
        return bool(value)
    return default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = " ".join(str(value).split())
    return text[:MAX_TEXT_LENGTH] if text else default


def _as_list(value: Any, *, lower: bool = False) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return []
    items: list[str] = []
    for item in value:
        text = _as_text(item)
        if lower:
            text = text.lower()
        if text and text not in items:
            items.append(text)
        if len(items) >= MAX_LIST_ITEMS:
            break
    return items


def normalize_precheck(raw: Any) -> dict[str, Any]:
    """Clamp a pre-check answer to the SPEC 6.2 shape."""
    if not isinstance(raw, dict):
        raise GeminiError("Pre-check answer was not a JSON object.")
    count = _as_int(raw.get("animal_count"), 0, 0, 99)
    contains = _as_bool(raw.get("contains_animal"), default=count > 0)
    # An unknown species word for a visible animal counts as "other" (then rejected).
    species = _as_choice(
        raw.get("species"), SPECIES_VALUES, "other" if contains or count else "none"
    )
    if species in ("dog", "cat") and count == 0:
        count = 1  # the model named a species, so at least one animal is visible
    return {
        "contains_animal": contains or count > 0,
        "species": species,
        "animal_count": count,
        "image_quality": _as_choice(raw.get("image_quality"), QUALITY_VALUES, "fair"),
        "quality_issues": _as_list(raw.get("quality_issues"), lower=True),
        "is_real_photo": _as_bool(raw.get("is_real_photo"), default=True),
    }


def normalize_attributes(raw: Any) -> dict[str, Any]:
    """Clamp an attribute answer to the SPEC 6.3 shape."""
    if not isinstance(raw, dict):
        raise GeminiError("Attribute answer was not a JSON object.")
    species = _as_choice(raw.get("species"), ("dog", "cat"), "")
    return {
        "species": species or None,
        "breed_guess": _as_text(raw.get("breed_guess"), "Unknown"),
        "primary_colors": _as_list(raw.get("primary_colors"), lower=True)[:3],
        "coat_pattern": _as_text(raw.get("coat_pattern"), "other").lower(),
        "coat_length": _as_choice(raw.get("coat_length"), COAT_LENGTH_VALUES, "unknown"),
        "size_estimate": _as_choice(raw.get("size_estimate"), SIZE_VALUES, "unknown"),
        "distinctive_marks": _as_list(raw.get("distinctive_marks")),
        "accessories": _as_list(raw.get("accessories")),
    }


def normalize_comparison(raw: Any) -> dict[str, Any]:
    """Clamp a comparison answer to the SPEC 6.5 shape (similarity 0-100)."""
    if not isinstance(raw, dict):
        raise GeminiError("Comparison answer was not a JSON object.")
    if raw.get("visual_similarity") is None:
        raise GeminiError("Comparison answer had no visual_similarity.")
    possible = _as_bool(raw.get("comparison_possible"), default=True)
    return {
        "visual_similarity": _as_int(raw.get("visual_similarity"), 0, 0, 100),
        "matching_features": _as_list(raw.get("matching_features")),
        "differences": _as_list(raw.get("differences")),
        "confidence": _as_choice(raw.get("confidence"), CONFIDENCE_VALUES, "low"),
        "comparison_possible": possible,
    }


def guess_mime_type(data: bytes) -> str:
    """MIME type from the file header (our stored images are JPEG)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class _BadAnswer(GeminiError):
    """The model answered, but not with usable JSON (retried like a transient error)."""


def is_retryable(exc: BaseException) -> bool:
    """429 / 408 / 5xx answers and network timeouts are worth another try."""
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None) in RETRYABLE_STATUS_CODES
    # httpx.TransportError covers timeouts and connection errors.
    return isinstance(exc, httpx.TransportError | _BadAnswer)


# --- The service ---------------------------------------------------------------------------


class GeminiService:
    """Thin wrapper around the google-genai client.

    Return shapes:

    * ``precheck(image_bytes)`` -> SPEC 6.2::

        {"contains_animal": bool, "species": "dog|cat|other|none", "animal_count": int,
         "image_quality": "good|fair|poor", "quality_issues": [str], "is_real_photo": bool}

    * ``extract_attributes(image_bytes)`` -> SPEC 6.3::

        {"species": "dog|cat"|None, "breed_guess": str, "primary_colors": [str],
         "coat_pattern": str, "coat_length": str, "size_estimate": "small|medium|large|unknown",
         "distinctive_marks": [str], "accessories": [str]}

    * ``compare(image_a, image_b)`` -> SPEC 6.5::

        {"visual_similarity": 0-100, "matching_features": [str], "differences": [str],
         "confidence": "low|medium|high", "comparison_possible": bool}

    Every method raises :class:`GeminiError` on failure
    (:class:`GeminiNotConfigured` when there is no API key).
    """

    #: Seconds before the 2nd attempt; doubled for each later attempt (plus a little jitter).
    backoff_base: float = 1.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg: dict[str, Any] = dict(current_app.config) if has_app_context() else {}
        self.api_key = api_key if api_key is not None else cfg.get("GEMINI_API_KEY")
        self.model = model or cfg.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.temperature = float(cfg.get("GEMINI_TEMPERATURE", 0.1))
        self.timeout_seconds = float(cfg.get("GEMINI_TIMEOUT_SECONDS", 30))
        self.max_attempts = max(1, int(cfg.get("GEMINI_MAX_ATTEMPTS", 3)))
        self.thinking_budget = cfg.get(
            "GEMINI_THINKING_BUDGET", _default_thinking_budget(self.model)
        )
        self._client = client
        self._client_lock = threading.Lock()
        self._sleep = sleep

    # -- public API ------------------------------------------------------------------------

    def is_configured(self) -> bool:
        """True when an API key is available (or a client was injected)."""
        return bool(self.api_key) or self._client is not None

    def precheck(self, image_bytes: bytes) -> dict[str, Any]:
        """Validation pre-check: exactly one dog/cat? species? photo quality? (SPEC 6.2)."""
        raw = self._generate([PRECHECK_PROMPT, _image_part(image_bytes)], PRECHECK_SCHEMA)
        return normalize_precheck(raw)

    def extract_attributes(self, image_bytes: bytes) -> dict[str, Any]:
        """Attribute extraction (SPEC 6.3). Each stored image is analysed once (callers cache)."""
        raw = self._generate([ATTRIBUTES_PROMPT, _image_part(image_bytes)], ATTRIBUTES_SCHEMA)
        return normalize_attributes(raw)

    def compare(self, image_a: bytes, image_b: bytes) -> dict[str, Any]:
        """Visual comparison of two images in one request (SPEC 6.5). Never claims same individual."""
        contents = [
            COMPARE_PROMPT,
            "IMAGE A:",
            _image_part(image_a),
            "IMAGE B:",
            _image_part(image_b),
        ]
        raw = self._generate(contents, COMPARE_SCHEMA)
        return normalize_comparison(raw)

    # -- internals ---------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The SDK client, created on first use (no network call happens here)."""
        if not self.is_configured():
            raise GeminiNotConfigured("GEMINI_API_KEY is not set.")
        with self._client_lock:
            if self._client is None:
                self._client = genai.Client(
                    api_key=self.api_key,
                    vertexai=False,
                    http_options=types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
                )
        return self._client

    def _config(self, schema: types.Schema) -> types.GenerateContentConfig:
        options: dict[str, Any] = {
            "temperature": self.temperature,
            "response_mime_type": "application/json",
            "response_schema": schema,
            "candidate_count": 1,
        }
        if self.thinking_budget is not None:
            options["thinking_config"] = types.ThinkingConfig(
                thinking_budget=int(self.thinking_budget)
            )
        return types.GenerateContentConfig(**options)

    def _generate(self, contents: list[Any], schema: types.Schema) -> dict[str, Any]:
        """One structured-output request with retries. Returns the parsed JSON object."""
        client = self.client
        config = self._config(schema)
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                return _parse_response(response)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if not is_retryable(exc) or attempt >= self.max_attempts:
                    break
                delay = self.backoff_base * (2 ** (attempt - 1))
                delay += random.uniform(0, self.backoff_base / 4) if delay else 0
                log.warning(
                    "Gemini call failed (attempt %s/%s: %s); retrying in %.1fs",
                    attempt,
                    self.max_attempts,
                    _describe(exc),
                    delay,
                )
                self._sleep(delay)
        raise GeminiError(f"Gemini request failed: {_describe(last_error)}") from last_error


def _default_thinking_budget(model: str) -> int | None:
    """Gemini 2.5 Flash models accept thinking_budget=0 (thinking off = faster, cheaper).
    Other models keep their default; override with the GEMINI_THINKING_BUDGET setting."""
    return 0 if model.startswith("gemini-2.5-flash") else None


def _image_part(data: bytes) -> types.Part:
    if not data:
        raise GeminiError("Empty image.")
    return types.Part.from_bytes(data=data, mime_type=guess_mime_type(data))


def _parse_response(response: Any) -> dict[str, Any]:
    """JSON object from a GenerateContentResponse, or _BadAnswer."""
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise GeminiError(f"Gemini blocked the request ({feedback.block_reason}).")
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    try:
        text = response.text
    except Exception as exc:  # noqa: BLE001 - SDK raises ValueError on odd candidates
        raise _BadAnswer(f"Unreadable answer: {exc}") from exc
    if not text:
        raise _BadAnswer("Empty answer.")
    text = text.strip()
    if text.startswith("```"):  # defensive: strip a Markdown fence if one slips through
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _BadAnswer("Answer was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise _BadAnswer("Answer was not a JSON object.")
    return data


def _describe(exc: BaseException | None) -> str:
    """Short error text for logs (never includes request data or the key)."""
    if exc is None:
        return "unknown error"
    if isinstance(exc, genai_errors.APIError):
        return f"HTTP {exc.code} {exc.status or ''}".strip()
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def get_gemini() -> GeminiService:
    """The service configured for the current app."""
    return GeminiService()
