"""gemini_service with a fake google-genai client (never the real API)."""

from __future__ import annotations

import json

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from app.services import gemini_service
from app.services.gemini_service import (
    COMPARE_PROMPT,
    COMPARE_SCHEMA,
    PRECHECK_SCHEMA,
    GeminiError,
    GeminiNotConfigured,
    GeminiService,
    normalize_attributes,
    normalize_comparison,
    normalize_precheck,
)

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


class FakeResponse:
    def __init__(self, text=None, parsed=None):
        self.text = text
        self.parsed = parsed
        self.prompt_feedback = None


class FakeModels:
    """Answers with the next item of ``script`` (a dict -> JSON text, an exception -> raised)."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(text=json.dumps(item) if not isinstance(item, str) else item)


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def _service(app, script, **kw) -> tuple[GeminiService, FakeClient, list[float]]:
    client = FakeClient(script)
    sleeps: list[float] = []
    service = GeminiService(api_key="test-key", client=client, sleep=sleeps.append, **kw)
    return service, client, sleeps


def _api_error(code: int, status: str) -> genai_errors.APIError:
    body = {"error": {"code": code, "message": "fake", "status": status}}
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, body)


GOOD_COMPARE = {
    "visual_similarity": 83,
    "matching_features": ["white chest blaze", "same ear shape"],
    "differences": ["collar colour"],
    "confidence": "high",
    "comparison_possible": True,
}


def test_not_configured(app):
    service = GeminiService(api_key="")
    assert service.is_configured() is False
    with pytest.raises(GeminiNotConfigured):
        service.precheck(JPEG)


def test_config_defaults_come_from_app_config(app):
    app.config.update(GEMINI_MODEL="gemini-2.5-flash", GEMINI_TEMPERATURE=0.15)
    service = GeminiService(api_key="k")
    assert service.model == "gemini-2.5-flash"
    assert service.temperature == 0.15
    assert service.max_attempts == app.config["GEMINI_MAX_ATTEMPTS"]
    assert service.is_configured()


def test_precheck_uses_structured_output_and_image_part(app):
    answer = {
        "contains_animal": True,
        "species": "cat",
        "animal_count": 1,
        "image_quality": "good",
        "quality_issues": [],
        "is_real_photo": True,
    }
    service, client, _ = _service(app, [answer])
    result = service.precheck(JPEG)
    assert result == answer

    request = client.models.requests[0]
    config: types.GenerateContentConfig = request["config"]
    assert request["model"] == service.model
    assert config.response_mime_type == "application/json"
    assert config.response_schema is PRECHECK_SCHEMA
    assert 0 <= config.temperature <= 0.2
    parts = [c for c in request["contents"] if isinstance(c, types.Part)]
    assert len(parts) == 1
    assert parts[0].inline_data.data == JPEG
    assert parts[0].inline_data.mime_type == "image/jpeg"


def test_compare_sends_both_images_in_one_request(app):
    service, client, _ = _service(app, [GOOD_COMPARE])
    assert service.compare(JPEG, PNG) == GOOD_COMPARE
    request = client.models.requests[0]
    assert request["config"].response_schema is COMPARE_SCHEMA
    parts = [c for c in request["contents"] if isinstance(c, types.Part)]
    assert [p.inline_data.mime_type for p in parts] == ["image/jpeg", "image/png"]
    texts = [c for c in request["contents"] if isinstance(c, str)]
    assert texts[0] == COMPARE_PROMPT
    assert all(isinstance(c, str | types.Part) for c in request["contents"])  # nothing else sent


def test_prompts_never_claim_identity():
    assert "Do NOT claim that the two animals are the same individual" in COMPARE_PROMPT
    for prompt in (
        gemini_service.PRECHECK_PROMPT,
        gemini_service.ATTRIBUTES_PROMPT,
        COMPARE_PROMPT,
    ):
        assert "Return only JSON" in prompt
        assert "people" in prompt  # tells the model not to describe people


def test_compare_values_are_clamped(app):
    wild = {
        "visual_similarity": 250,
        "matching_features": "coat",  # a string instead of a list
        "differences": ["a"] * 30,
        "confidence": "VERY HIGH",
        "comparison_possible": "true",
    }
    service, _, _ = _service(app, [wild])
    result = service.compare(JPEG, JPEG)
    assert result["visual_similarity"] == 100
    assert result["matching_features"] == ["coat"]
    assert len(result["differences"]) <= gemini_service.MAX_LIST_ITEMS
    assert result["confidence"] == "low"
    assert result["comparison_possible"] is True


@pytest.mark.parametrize(("raw", "expected"), [(-4, 0), ("87", 87), (55.6, 56), (100, 100)])
def test_similarity_clamping(raw, expected):
    assert normalize_comparison({"visual_similarity": raw})["visual_similarity"] == expected


def test_comparison_without_score_is_an_error():
    with pytest.raises(GeminiError):
        normalize_comparison({"matching_features": []})
    with pytest.raises(GeminiError):
        normalize_comparison(["not", "a", "dict"])


def test_precheck_normalisation():
    result = normalize_precheck({"species": "DOG", "animal_count": "0", "image_quality": "meh"})
    assert result["species"] == "dog"
    assert result["animal_count"] == 1  # species named -> at least one animal
    assert result["contains_animal"] is True
    assert result["image_quality"] == "fair"
    assert result["is_real_photo"] is True
    odd = normalize_precheck({"species": "horse", "animal_count": 3, "contains_animal": True})
    assert odd["species"] == "other"
    assert odd["animal_count"] == 3


def test_attribute_normalisation():
    result = normalize_attributes(
        {
            "species": "Cat",
            "breed_guess": "  Puspin   (mixed) ",
            "primary_colors": ["Orange", "white", "orange", "black", "gray"],
            "coat_pattern": "Tabby",
            "coat_length": "SHORT",
            "size_estimate": "tiny",
            "distinctive_marks": None,
        }
    )
    assert result["species"] == "cat"
    assert result["breed_guess"] == "Puspin (mixed)"
    assert result["primary_colors"] == ["orange", "white", "black"]
    assert result["coat_pattern"] == "tabby"
    assert result["coat_length"] == "short"
    assert result["size_estimate"] == "unknown"
    assert result["distinctive_marks"] == [] and result["accessories"] == []


def test_retries_429_then_succeeds_with_exponential_backoff(app):
    service, client, sleeps = _service(
        app, [_api_error(429, "RESOURCE_EXHAUSTED"), _api_error(503, "UNAVAILABLE"), GOOD_COMPARE]
    )
    assert service.compare(JPEG, JPEG)["visual_similarity"] == 83
    assert len(client.models.requests) == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0] >= service.backoff_base


def test_gives_up_after_max_attempts(app):
    app.config["GEMINI_MAX_ATTEMPTS"] = 3
    errors = [_api_error(500, "INTERNAL") for _ in range(5)]
    service, client, sleeps = _service(app, errors)
    with pytest.raises(GeminiError) as err:
        service.compare(JPEG, JPEG)
    assert len(client.models.requests) == 3
    assert len(sleeps) == 2
    assert "500" in str(err.value)
    assert "test-key" not in str(err.value)


def test_timeouts_are_retried(app):
    service, client, _ = _service(app, [httpx.ReadTimeout("slow"), GOOD_COMPARE])
    assert service.compare(JPEG, JPEG)["confidence"] == "high"
    assert len(client.models.requests) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_errors_are_not_retried(app, code):
    service, client, sleeps = _service(app, [_api_error(code, "BAD"), GOOD_COMPARE])
    with pytest.raises(GeminiError):
        service.compare(JPEG, JPEG)
    assert len(client.models.requests) == 1
    assert sleeps == []


def test_invalid_json_is_retried_then_fails(app):
    service, client, _ = _service(app, ["not json", "```json\n[1,2]\n```", ""])
    with pytest.raises(GeminiError):
        service.precheck(JPEG)
    assert len(client.models.requests) == 3


def test_markdown_fenced_json_is_accepted(app):
    fenced = "```json\n" + json.dumps(GOOD_COMPARE) + "\n```"
    service, _, _ = _service(app, [fenced])
    assert service.compare(JPEG, JPEG)["visual_similarity"] == 83


def test_parsed_dict_is_used_when_present(app):
    service, _, _ = _service(app, [FakeResponse(text=None, parsed=dict(GOOD_COMPARE))])
    assert service.compare(JPEG, JPEG)["visual_similarity"] == 83


def test_blocked_prompt_is_an_error(app):
    blocked = FakeResponse(text=None)
    blocked.prompt_feedback = types.GenerateContentResponsePromptFeedback(block_reason="SAFETY")
    service, client, _ = _service(app, [blocked])
    with pytest.raises(GeminiError):
        service.precheck(JPEG)
    assert len(client.models.requests) == 1


def test_empty_image_is_rejected_before_calling(app):
    service, client, _ = _service(app, [GOOD_COMPARE])
    with pytest.raises(GeminiError):
        service.compare(b"", JPEG)
    assert client.models.requests == []


def test_thinking_budget_off_for_25_flash_only(app):
    flash, _, _ = _service(app, [], model="gemini-2.5-flash")
    other, _, _ = _service(app, [], model="gemini-3-flash-preview")
    assert flash._config(COMPARE_SCHEMA).thinking_config.thinking_budget == 0
    assert other._config(COMPARE_SCHEMA).thinking_config is None
    app.config["GEMINI_THINKING_BUDGET"] = 128
    tuned, _, _ = _service(app, [], model="gemini-3-flash-preview")
    assert tuned._config(COMPARE_SCHEMA).thinking_config.thinking_budget == 128


def test_sdk_client_is_built_with_key_and_timeout(app, monkeypatch):
    built: dict = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(gemini_service.genai, "Client", RecordingClient)
    app.config["GEMINI_TIMEOUT_SECONDS"] = 12
    service = GeminiService(api_key="abc")
    assert isinstance(service.client, RecordingClient)
    assert service.client is service.client  # created once
    assert built["api_key"] == "abc"
    assert built["vertexai"] is False
    assert built["http_options"].timeout == 12_000  # milliseconds


def test_guess_mime_type():
    assert gemini_service.guess_mime_type(JPEG) == "image/jpeg"
    assert gemini_service.guess_mime_type(PNG) == "image/png"
    assert gemini_service.guess_mime_type(b"RIFF1234WEBPxxxx") == "image/webp"
