"""matching_service: privacy pools, scoring, run_scan, caching, fallbacks, background matching."""

from __future__ import annotations

import io
import threading
from datetime import timedelta

import pytest
import sqlalchemy as sa
from conftest import TACLOBAN, jpeg_bytes
from PIL import Image

from app.extensions import db
from app.models import (
    CandidateType,
    ImageComparison,
    LostReport,
    MatchTier,
    Photo,
    PhotoOwner,
    Scan,
    ScanResult,
    ScanStatus,
    ScanTrigger,
    Sighting,
)
from app.services import matching_service as ms
from app.services import notification_service, policy_service, storage_service
from app.services.job_service import enqueue
from app.utils.time import utcnow

RED, BLUE, GREEN = (220, 30, 30), (30, 60, 220), (40, 180, 60)
WEIGHTS = {"visual": 0.70, "attribute": 0.15, "distance": 0.10, "recency": 0.05}


# --- helpers ------------------------------------------------------------------------------


def _center_color(data: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        return rgb.getpixel((rgb.width // 2, rgb.height // 2))


def _same_color(a: tuple, b: tuple) -> bool:
    return all(abs(x - y) < 30 for x, y in zip(a, b, strict=True))


def color_compare(scores: dict[tuple, int], query_color=RED):
    """compare_fn giving a visual score chosen by the candidate photo's colour."""

    def compare(image_a: bytes, image_b: bytes) -> dict:
        cand = _center_color(image_b)
        score = next((s for c, s in scores.items() if _same_color(c, cand)), 0)
        return {
            "visual_similarity": score,
            "matching_features": [f"score {score}"],
            "differences": [],
            "confidence": "medium",
            "comparison_possible": True,
        }

    return compare


def scan_photo(color=RED) -> Photo:
    return storage_service.save_image(
        jpeg_bytes(color),
        owner_type=PhotoOwner.SCAN,
        folder=storage_service.default_folder(PhotoOwner.SCAN),
    )


@pytest.fixture
def report_with_color(make_lost_report, make_photo):
    def _make(color=RED, **fields) -> LostReport:
        report = make_lost_report(photos=0, **fields)
        make_photo(PhotoOwner.LOST_REPORT, report.id, color=color)
        return report

    return _make


@pytest.fixture
def sighting_with_color(make_sighting, make_photo):
    def _make(color=RED, **fields) -> Sighting:
        sighting = make_sighting(photos=0, **fields)
        make_photo(PhotoOwner.SIGHTING, sighting.id, color=color)
        return sighting

    return _make


@pytest.fixture
def notified(monkeypatch):
    """Record notify() calls instead of creating notifications."""
    calls: list[tuple[int, str, dict]] = []

    def record(user, event, data=None):
        calls.append((user.id, event, dict(data or {})))

    monkeypatch.setattr(notification_service, "notify", record)
    return calls


def compare_calls(fake) -> int:
    return sum(1 for call in fake.calls if call[0] == "compare")


# =============================================================================================
# Candidate pool - privacy rules (SPEC 6.4)
# =============================================================================================


def test_found_pool_only_public_lost_reports(app, make_user, make_lost_report, make_sighting):
    finder = make_user()
    visible = make_lost_report()
    make_lost_report(status="found")
    make_lost_report(status="closed")
    make_lost_report(is_hidden=True, hidden_reason="spam")
    make_sighting()  # sightings are NEVER in the found pool

    pool = ms.get_candidate_pool("found", finder)
    assert [r.id for r in pool] == [visible.id]
    assert all(isinstance(r, LostReport) for r in pool)


def test_lost_pool_only_active_visible_unexpired_sightings(
    app, make_user, make_sighting, make_lost_report
):
    owner = make_user()
    visible = make_sighting()
    make_sighting(status="matched")
    make_sighting(status="expired")
    make_sighting(is_hidden=True)
    stale = make_sighting()
    stale.expires_at = utcnow() - timedelta(minutes=1)  # expiry job has not run yet
    db.session.commit()
    make_lost_report()  # reports are never in the lost pool

    pool = ms.get_candidate_pool("lost", owner)
    assert [s.id for s in pool] == [visible.id]
    assert all(isinstance(s, Sighting) for s in pool)


def test_own_records_are_excluded(app, make_user, make_lost_report, make_sighting):
    me = make_user()
    make_lost_report(owner=me)
    make_sighting(finder=me)
    other_report = make_lost_report()
    other_sighting = make_sighting()

    assert [r.id for r in ms.get_candidate_pool("found", me)] == [other_report.id]
    assert [s.id for s in ms.get_candidate_pool("lost", me)] == [other_sighting.id]
    # The CLI (no user) sees everything searchable.
    assert len(ms.get_candidate_pool("found", None)) == 2


def test_pool_filters_species_radius_and_date(app, make_user, make_lost_report):
    user = make_user()
    near_dog = make_lost_report(species="dog")
    make_lost_report(species="cat")
    far = make_lost_report(last_seen_lat=TACLOBAN[0] + 0.3)  # ~33 km north
    make_lost_report(last_seen_at=utcnow() - timedelta(days=90))  # outside the 60-day window

    pool = ms.get_candidate_pool("found", user, species="dog", lat=TACLOBAN[0], lng=TACLOBAN[1])
    assert [r.id for r in pool] == [near_dog.id]
    no_location = ms.get_candidate_pool("found", user, species="dog")
    assert {r.id for r in no_location} == {near_dog.id, far.id}
    wide = ms.get_candidate_pool("found", user, species="dog", window_days=120)
    assert len(wide) == 3


def test_pool_rejects_unknown_mode(app, user):
    with pytest.raises(ValueError):
        ms.get_candidate_pool("everything", user)


# =============================================================================================
# Scoring math
# =============================================================================================


def test_color_and_pattern_parsing():
    assert ms.color_set("Brown and white") == {"brown", "white"}
    assert ms.color_set(["Ginger", "cream"]) == {"orange", "tan"}
    assert ms.color_set("Kulay itim at puti") == {"black", "white"}
    assert ms.color_set(None) == set()
    assert ms.pattern_of("Orange tabby") == "tabby"
    assert ms.pattern_of("brown") is None


def test_profile_prefers_form_fields_over_gemini():
    ai = {
        "species": "dog",
        "primary_colors": ["black"],
        "size_estimate": "large",
        "coat_length": "long",
    }
    profile = ms.build_profile(ai=ai, species="dog", colors_text="Brown and white", size="small")
    assert profile.colors == {"brown", "white"}
    assert profile.size == "small"
    assert profile.pattern == "bicolor"  # inferred from two colours
    assert profile.coat_length == "long"  # only Gemini knows


def test_attribute_score():
    a = ms.Profile(colors={"brown", "white"}, size="medium", pattern="bicolor", coat_length="short")
    assert ms.attribute_score(a, a) == 100.0
    opposite = ms.Profile(colors={"black"}, size="large", pattern="solid", coat_length="long")
    assert ms.attribute_score(a, opposite) == pytest.approx(100 * 0.25 * 0.5 / 1.0)
    assert ms.attribute_score(a, ms.Profile()) == ms.NEUTRAL_SCORE
    score, same, diff = ms.attribute_comparison(a, ms.Profile(colors={"brown"}, size="medium"))
    assert 0 < score < 100
    assert any("brown" in s for s in same) and any("size" in s.lower() for s in same)
    assert any("colours differ" in d for d in diff)


@pytest.mark.parametrize(("km", "expected"), [(0, 100), (7.5, 50), (15, 0), (40, 0), (None, 50)])
def test_distance_score(km, expected):
    assert ms.distance_score(km, 15) == expected


def test_recency_score():
    now = utcnow()
    assert ms.recency_score(now, 60, now) == 100
    assert ms.recency_score(now - timedelta(days=30), 60, now) == 50
    assert ms.recency_score(now - timedelta(days=90), 60, now) == 0
    assert ms.recency_score(None, 60, now) == 50


def test_final_score_formula_and_fallback():
    # SPEC 6.6: 0.70*80 + 0.15*60 + 0.10*100 + 0.05*100 = 80
    assert ms.final_score(80, 60, 100, 100, WEIGHTS) == 80.0
    assert ms.final_score(100, 100, 100, 100, WEIGHTS) == 100.0
    coarse = ms.coarse_score(60, 100, 100, WEIGHTS)
    assert coarse == pytest.approx((0.15 * 60 + 0.10 * 100 + 0.05 * 100) / 0.30, abs=0.1)
    # No visual check: coarse * 0.6, so it can never reach "high" or the notify threshold.
    assert ms.final_score(None, 100, 100, 100, WEIGHTS) == 60.0


def test_weights_come_from_config(app):
    app.config["MATCH_WEIGHTS"] = {"visual": 1.0, "attribute": 0, "distance": 0, "recency": 0}
    assert ms.final_score(42, 100, 100, 100) == 42.0


@pytest.mark.parametrize(
    ("score", "tier"),
    [
        (100, "high"),
        (80, "high"),
        (79.9, "moderate"),
        (60, "moderate"),
        (40, "low"),
        (39.9, "hidden"),
        (0, "hidden"),
    ],
)
def test_tiers(app, score, tier):
    assert ms.tier_for(score) == tier


def test_tier_labels(app):
    assert ms.tier_label("high") == "High visual similarity"
    assert ms.tier_label("hidden") == app.config["MATCH_HIDDEN_LABEL"]


# =============================================================================================
# run_scan
# =============================================================================================


def test_found_scan_ranks_persists_and_attaches_photo(app, user, fake_gemini, report_with_color):
    best = report_with_color(RED)
    middle = report_with_color(GREEN)
    weak = report_with_color(BLUE)
    fake_gemini.compare_fn = color_compare({RED: 95, GREEN: 65, BLUE: 5})
    photo = scan_photo(RED)

    scan = ms.run_scan(user=user, mode="found", photo=photo, lat=TACLOBAN[0], lng=TACLOBAN[1])

    assert scan.status == ScanStatus.COMPLETE
    assert scan.trigger == ScanTrigger.MANUAL
    assert scan.gemini_ok is True
    assert scan.precheck["species"] == "dog"
    assert scan.attributes["breed_guess"] == "Aspin (mixed)"
    assert photo.owner_type == PhotoOwner.SCAN and photo.owner_id == scan.id
    assert photo.ai_attributes == scan.attributes  # cached on the photo
    results = list(scan.results)
    assert [r.candidate_id for r in results] == [best.id, middle.id, weak.id]
    assert [r.rank for r in results] == [1, 2, 3]
    top = results[0]
    assert top.candidate_type == CandidateType.LOST_REPORT
    assert top.visual_score == 95
    assert top.visual_check_unavailable is False
    assert top.tier == MatchTier.HIGH
    assert top.distance_km is not None and top.distance_km < 0.1
    expected = ms.final_score(95, top.attribute_score, top.distance_score, top.recency_score)
    assert top.final_score == expected
    assert top.explanation["matching_features"] == ["score 95"]
    assert results[2].tier == MatchTier.HIDDEN
    assert scan.result_count == 2  # results at or above the relevance threshold
    assert db.session.get(Scan, scan.id) is not None  # committed


def test_compare_calls_run_in_worker_threads(app, user, fake_gemini, report_with_color):
    for color in (RED, GREEN, BLUE):
        report_with_color(color)
    threads: set[str] = set()
    inner = color_compare({RED: 90})

    def compare(a, b):
        threads.add(threading.current_thread().name)
        return inner(a, b)

    fake_gemini.compare_fn = compare
    ms.run_scan(user=user, mode="found", photo=scan_photo())
    assert threads and threading.main_thread().name not in threads


def test_top_n_limits_visual_comparisons(app, user, fake_gemini, report_with_color):
    app.config["MATCH_TOP_N"] = 2
    for color in (RED, GREEN, BLUE):
        report_with_color(color)
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo())
    assert compare_calls(fake_gemini) == 2
    assert len(scan.results) == 2


def test_comparisons_are_cached(app, user, fake_gemini, report_with_color):
    report_with_color(RED)
    report_with_color(BLUE)
    photo = scan_photo()
    ms.run_scan(user=user, mode="found", photo=photo)
    assert compare_calls(fake_gemini) == 2
    assert db.session.scalar(sa.select(sa.func.count(ImageComparison.id))) == 2

    fake_gemini.calls.clear()
    again = ms.run_scan(user=user, mode="found", photo=photo)  # same query photo (same sha256)
    assert compare_calls(fake_gemini) == 0
    assert all(r.visual_score == 85 for r in again.results)
    assert not any(c[0] == "extract_attributes" for c in fake_gemini.calls)  # cached on Photo

    app.config["GEMINI_MODEL"] = "gemini-other-model"  # cache is per model
    ms.run_scan(user=user, mode="found", photo=photo)
    assert compare_calls(fake_gemini) == 2


def test_gemini_compare_failure_falls_back_to_coarse(app, user, fake_gemini, report_with_color):
    report_with_color(RED)
    fake_gemini.fail = {"compare"}
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo())
    result = scan.results[0]
    assert scan.status == ScanStatus.COMPLETE
    assert scan.gemini_ok is False
    assert result.visual_check_unavailable is True
    assert result.visual_score is None
    coarse = ms.coarse_score(result.attribute_score, result.distance_score, result.recency_score)
    assert result.final_score == round(coarse * ms.COARSE_ONLY_FACTOR, 1)
    assert result.final_score <= 60
    assert (
        db.session.scalar(sa.select(sa.func.count(ImageComparison.id))) == 0
    )  # failures not cached


def test_comparison_not_possible_is_treated_as_unavailable(
    app, user, fake_gemini, report_with_color
):
    report_with_color(RED)
    fake_gemini.compare_fn = lambda a, b: {
        "visual_similarity": 99,
        "matching_features": [],
        "differences": [],
        "confidence": "low",
        "comparison_possible": False,
    }
    result = ms.run_scan(user=user, mode="found", photo=scan_photo()).results[0]
    assert result.visual_check_unavailable is True
    assert result.final_score <= 60


def test_without_gemini_key_scan_still_works(app, user, report_with_color):
    report = report_with_color(RED)  # no fake_gemini: GEMINI_API_KEY is None in tests
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo(), species="dog")
    assert scan.status == ScanStatus.COMPLETE
    assert scan.gemini_ok is False
    assert scan.precheck is None
    assert [r.candidate_id for r in scan.results] == [report.id]
    assert scan.results[0].visual_check_unavailable is True


def test_species_hint_used_only_without_gemini(app, user, report_with_color):
    dog = report_with_color(RED, species="dog")
    cat = report_with_color(RED, species="cat")
    hinted = ms.run_scan(user=user, mode="found", photo=scan_photo(), species="cat")
    assert [r.candidate_id for r in hinted.results] == [cat.id]
    unknown = ms.run_scan(user=user, mode="found", photo=scan_photo())
    assert {r.candidate_id for r in unknown.results} == {dog.id, cat.id}


def test_species_filter_from_precheck(app, user, fake_gemini, report_with_color):
    report_with_color(RED, species="dog")
    cat = report_with_color(RED, species="cat")
    fake_gemini.precheck_result = dict(fake_gemini.precheck_result, species="cat")
    fake_gemini.attributes_result = dict(fake_gemini.attributes_result, species="cat")
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo())
    assert [r.candidate_id for r in scan.results] == [cat.id]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"species": "other"}, "not_dog_or_cat"),
        ({"species": "none", "contains_animal": False, "animal_count": 0}, "no_animal"),
        ({"animal_count": 2}, "multiple_animals"),
        ({"is_real_photo": False}, "not_a_photo"),
    ],
)
def test_precheck_rejections_are_persisted(
    app, user, fake_gemini, report_with_color, changes, code
):
    report_with_color(RED)
    fake_gemini.precheck_result = dict(fake_gemini.precheck_result, **changes)
    photo = scan_photo()
    with pytest.raises(ms.ScanRejected) as err:
        ms.run_scan(user=user, mode="found", photo=photo)
    assert err.value.code == code
    assert err.value.message == ms.REJECTION_MESSAGES[code]
    assert err.value.details["species"] == fake_gemini.precheck_result["species"]
    scan = db.session.get(Scan, err.value.scan_id)
    assert scan.status == ScanStatus.REJECTED
    assert scan.query_photo_id == photo.id
    assert scan.precheck == err.value.details
    assert scan.results == []
    assert compare_calls(fake_gemini) == 0


def test_poor_quality_continues_with_warning(app, user, fake_gemini, report_with_color):
    report_with_color(RED)
    fake_gemini.precheck_result = dict(
        fake_gemini.precheck_result, image_quality="poor", quality_issues=["blurry"]
    )
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo())
    assert scan.status == ScanStatus.COMPLETE
    assert "blurry" in ms.quality_warning(scan)


def test_precheck_failure_does_not_block_scan(app, user, fake_gemini, report_with_color):
    report_with_color(RED)
    fake_gemini.fail = {"precheck"}
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo())
    assert scan.status == ScanStatus.COMPLETE
    assert scan.gemini_ok is False
    assert scan.results[0].visual_check_unavailable is False  # compare still worked


def test_unreadable_query_photo_is_rejected(app, user, fake_gemini):
    photo = scan_photo()
    storage_service.local_path(photo.public_id).unlink()
    with pytest.raises(ms.ScanRejected) as err:
        ms.run_scan(user=user, mode="found", photo=photo)
    assert err.value.code == "photo_unreadable"


def test_lost_scan_with_own_report_returns_only_other_sightings(
    app, make_user, fake_gemini, make_lost_report, sighting_with_color
):
    owner = make_user()
    report = make_lost_report(owner=owner)
    sighting = sighting_with_color(RED)
    sighting_with_color(RED, finder=owner)  # the owner's own sighting
    sighting_with_color(RED, seen_at=report.last_seen_at - timedelta(days=10))  # before the loss

    scan = ms.run_scan(
        user=owner, mode="lost", photo=report.primary_photo, source_lost_report=report
    )
    assert [r.candidate_id for r in scan.results] == [sighting.id]
    assert all(r.candidate_type == CandidateType.SIGHTING for r in scan.results)
    assert scan.source_lost_report_id == report.id
    assert (scan.scan_lat, scan.scan_lng) == (report.last_seen_lat, report.last_seen_lng)
    assert report.primary_photo.owner_type == PhotoOwner.LOST_REPORT  # not re-attached
    assert db.session.get(LostReport, report.id).ai_attributes  # copied from the primary photo
    assert policy_service.can_view_sighting(owner, sighting)  # the scan result grants access


def test_results_for_display(app, user, fake_gemini, report_with_color):
    strong = report_with_color(RED, pet_name="Bantay")
    report_with_color(BLUE)
    fake_gemini.compare_fn = color_compare({RED: 92, BLUE: 3})
    scan = ms.run_scan(user=user, mode="found", photo=scan_photo())

    with app.test_request_context():
        rows = ms.results_for_display(scan)
        weak_rows = ms.results_for_display(scan, include_weak=True)
        assert len(rows) == 1 and len(weak_rows) == 2
        row = rows[0]
        for key in (
            "result_id",
            "candidate_type",
            "candidate_id",
            "final_score",
            "visual_check_unavailable",
            "title",
            "detail_url",
            "photo_url",
            "similarity",
            "tier",
            "tier_label",
            "why",
        ):
            assert key in row
        assert row["candidate_id"] == strong.id
        assert row["title"] == "Bantay"
        assert row["tier_label"] == "High visual similarity"
        assert row["similarity"] == round(row["final_score"])

        strong.is_hidden = True  # moderated after the scan: never shown again
        db.session.commit()
        assert all(r["candidate_id"] != strong.id for r in ms.results_for_display(scan, True))


def test_results_for_display_sighting_links(
    app, make_user, fake_gemini, make_lost_report, sighting_with_color
):
    owner = make_user()
    report = make_lost_report(owner=owner)
    sighting = sighting_with_color(RED)
    scan = ms.run_scan(
        user=owner, mode="lost", photo=report.primary_photo, source_lost_report=report
    )
    with app.test_request_context():
        row = ms.results_for_display(scan)[0]
    assert row["candidate_type"] == CandidateType.SIGHTING
    assert row["detail_url"] == f"/sightings/{sighting.id}"
    assert f"sighting_id={sighting.id}" in row["message_url"]
    assert f"lost_report_id={report.id}" in row["message_url"]


# =============================================================================================
# Background matching and notifications
# =============================================================================================


def test_new_lost_report_background_match_notifies_owner_once(
    app, make_user, fake_gemini, notified, make_lost_report, sighting_with_color
):
    owner, finder = make_user(), make_user()
    report = make_lost_report(owner=owner)
    sighting = sighting_with_color(RED, finder=finder)
    fake_gemini.compare_score = 95

    scan = ms.match_new_lost_report(report.id)
    assert scan.trigger == ScanTrigger.BACKGROUND
    assert scan.user_id == owner.id  # SPEC 17.6: the owner being notified
    assert scan.precheck is None  # no pre-check for background scans
    assert [r.candidate_id for r in scan.results] == [sighting.id]
    assert scan.results[0].notified_at is not None
    assert len(notified) == 1
    user_id, event, data = notified[0]
    assert (user_id, event) == (owner.id, "possible_match")
    assert data["scan_id"] == scan.id
    assert data["lost_report_id"] == report.id
    assert data["sighting_id"] == sighting.id
    assert data["similarity"] >= app.config["MATCH_NOTIFY_THRESHOLD"]
    assert data["link_url"] == f"/scans/{scan.id}"
    assert "possible match" in data["title"].lower()

    ms.match_new_lost_report(report.id)  # job re-run: no second notification
    assert len(notified) == 1


def test_background_match_below_threshold_does_not_notify(
    app, make_user, fake_gemini, notified, make_lost_report, sighting_with_color
):
    report = make_lost_report()
    sighting_with_color(RED)
    fake_gemini.compare_score = 50
    scan = ms.match_new_lost_report(report.id)
    assert scan.results and scan.results[0].final_score < app.config["MATCH_NOTIFY_THRESHOLD"]
    assert notified == []


def test_background_scan_keeps_only_relevant_results(
    app, fake_gemini, notified, make_lost_report, sighting_with_color
):
    report = make_lost_report()
    sighting_with_color(RED)
    sighting_with_color(BLUE)
    fake_gemini.compare_fn = color_compare({RED: 90, BLUE: 0})
    scan = ms.match_new_lost_report(report.id)
    assert len(scan.results) == 1  # the weak sighting is not kept (no access granted)


def test_coarse_only_background_match_never_notifies(
    app, notified, make_lost_report, sighting_with_color
):
    report = make_lost_report()  # no fake_gemini -> no API key
    sighting_with_color(RED, colors="Brown and white")
    scan = ms.match_new_lost_report(report.id)
    assert scan.gemini_ok is False
    assert notified == []


def test_new_sighting_notifies_report_owner_with_private_scan(
    app, make_user, fake_gemini, notified, make_lost_report, sighting_with_color
):
    owner, finder = make_user(), make_user()
    report = make_lost_report(owner=owner)
    other_report = make_lost_report()
    sighting = sighting_with_color(RED, finder=finder)
    assert not policy_service.can_view_sighting(owner, sighting)
    fake_gemini.compare_score = 92

    scans = ms.match_new_sighting(sighting.id)

    assert {s.user_id for s in scans} == {owner.id, other_report.owner_id}
    owner_scan = next(s for s in scans if s.user_id == owner.id)
    assert owner_scan.trigger == ScanTrigger.BACKGROUND
    assert owner_scan.mode == "lost"
    assert owner_scan.source_lost_report_id == report.id
    assert owner_scan.query_photo_id == report.primary_photo.id
    result = owner_scan.results[0]
    assert (result.candidate_type, result.candidate_id) == (CandidateType.SIGHTING, sighting.id)
    assert result.notified_at is not None
    assert policy_service.can_view_sighting(owner, sighting)  # access via the owner's scan
    # The finder gets no scan and so never sees the owners' reports automatically.
    assert (
        db.session.scalar(sa.select(sa.func.count(Scan.id)).where(Scan.user_id == finder.id)) == 0
    )
    assert sorted(n[0] for n in notified) == sorted([owner.id, other_report.owner_id])
    assert all(n[2]["sighting_id"] == sighting.id for n in notified)
    # Attributes were extracted once and copied to the sighting.
    assert db.session.get(Sighting, sighting.id).ai_attributes

    assert ms.match_new_sighting(sighting.id) == []  # re-run: nothing new
    ms.match_new_lost_report(report.id)  # other path, same pair: still no duplicate
    assert len(notified) == 2


def test_new_sighting_ignores_finders_own_report_and_other_species(
    app, make_user, fake_gemini, notified, make_lost_report, sighting_with_color
):
    finder = make_user()
    make_lost_report(owner=finder)
    make_lost_report(species="cat")
    sighting = sighting_with_color(RED, finder=finder)
    fake_gemini.compare_score = 99
    assert ms.match_new_sighting(sighting.id) == []
    assert notified == []


def test_background_match_skips_unsearchable_records(
    app, fake_gemini, make_lost_report, make_sighting
):
    assert ms.match_new_lost_report(make_lost_report(status="found").id) is None
    assert ms.match_new_lost_report(make_lost_report(photos=0).id) is None
    assert ms.match_new_lost_report(999) is None
    assert ms.match_new_sighting(make_sighting(is_hidden=True).id) == []
    assert ms.match_new_sighting(make_sighting(photos=0).id) == []
    assert ms.match_new_sighting(999) == []


def test_jobs_run_inline_and_through_the_worker(
    app, fake_gemini, notified, make_lost_report, sighting_with_color, run_jobs
):
    report = make_lost_report()
    sighting = sighting_with_color(RED)
    fake_gemini.compare_score = 95
    enqueue("match_sighting", {"sighting_id": sighting.id})  # JOBS_EAGER in tests
    db.session.commit()
    assert len(notified) == 1
    assert notified[0][2]["lost_report_id"] == report.id


@pytest.mark.config(JOBS_EAGER=False)
def test_match_lost_report_job_via_worker(
    app, fake_gemini, notified, make_lost_report, sighting_with_color, run_jobs
):
    report = make_lost_report()
    sighting_with_color(RED)
    fake_gemini.compare_score = 95
    enqueue("match_lost_report", {"lost_report_id": report.id})
    db.session.commit()
    assert notified == []
    assert run_jobs() == 1
    assert len(notified) == 1


def test_extract_attributes_job_caches_on_photo_and_report(app, fake_gemini, make_lost_report):
    report = make_lost_report()
    photo = report.primary_photo
    enqueue("extract_attributes", {"photo_id": photo.id})
    db.session.commit()
    assert db.session.get(Photo, photo.id).ai_attributes["breed_guess"] == "Aspin (mixed)"
    assert db.session.get(LostReport, report.id).ai_attributes["coat_pattern"] == "bicolor"

    fake_gemini.calls.clear()
    ms.extract_attributes_for_photo(photo.id)
    assert fake_gemini.calls == []  # each stored image is analysed once


def test_extract_attributes_failure_is_quiet(app, fake_gemini, make_lost_report):
    report = make_lost_report()
    fake_gemini.fail = {"extract_attributes"}
    assert ms.extract_attributes_for_photo(report.primary_photo.id) is None
    assert ms.extract_attributes_for_photo(999) is None


def test_cleanup_scan_photos(app, user, make_lost_report):
    old = scan_photo()
    recent = scan_photo()
    scan = Scan(user_id=user.id, mode="found", query_photo_id=old.id, status="complete")
    db.session.add(scan)
    old.created_at = utcnow() - timedelta(days=app.config["SCAN_PHOTO_RETENTION_DAYS"] + 1)
    report = make_lost_report()
    report.primary_photo.created_at = utcnow() - timedelta(days=400)
    db.session.commit()
    old_path = storage_service.local_path(old.public_id)

    enqueue("cleanup_scan_photos", {})
    db.session.commit()

    assert db.session.get(Photo, old.id) is None
    assert not old_path.exists()
    assert db.session.get(Photo, recent.id) is not None
    assert db.session.get(Scan, scan.id).query_photo_id is None
    assert report.primary_photo is not None  # report photos are never cleaned up
    assert ms.cleanup_scan_photos() == 0


# =============================================================================================
# Command-line tools
# =============================================================================================


def test_cli_match_test_with_gemini(app, fake_gemini, report_with_color, tmp_path, capsys):
    report = report_with_color(RED)
    photo = tmp_path / "found_dog.jpg"
    photo.write_bytes(jpeg_bytes(RED))
    assert ms.cli_match_test(photo, "found", TACLOBAN[0], TACLOBAN[1]) == 0
    out = capsys.readouterr().out
    assert "Pre-check" in out and "Attributes" in out
    assert report.case_number in out
    assert "visual indicators only" in out
    assert db.session.scalar(sa.select(sa.func.count(Scan.id))) == 0  # nothing saved


def test_cli_match_test_without_gemini_and_bad_file(app, report_with_color, tmp_path, capsys):
    report_with_color(RED)
    photo = tmp_path / "dog.jpg"
    photo.write_bytes(jpeg_bytes(RED))
    assert ms.cli_match_test(photo, "found") == 0
    out = capsys.readouterr().out
    assert "GEMINI_API_KEY is not set" in out
    assert "n/a" in out  # no visual score

    bad = tmp_path / "notes.jpg"
    bad.write_text("hello")
    assert ms.cli_match_test(bad) == 1


def test_cli_match_test_reports_rejection(app, fake_gemini, tmp_path, capsys):
    fake_gemini.precheck_result = dict(fake_gemini.precheck_result, animal_count=3)
    photo = tmp_path / "dogs.jpg"
    photo.write_bytes(jpeg_bytes(RED))
    assert ms.cli_match_test(photo) == 3
    assert "multiple_animals" in capsys.readouterr().out


def test_find_pairs(tmp_path):
    same = tmp_path / "same"
    (same / "bantay").mkdir(parents=True)
    for name in (
        "bantay/1.jpg",
        "bantay/2.jpg",
        "mingming_a.jpg",
        "mingming_b.png",
        "lonely_a.jpg",
    ):
        (same / name).write_bytes(b"x")
    pairs = ms.find_pairs(same)
    assert len(pairs) == 2
    assert ms.find_pairs(tmp_path / "missing") == []


def test_suggest_threshold():
    assert ms.suggest_threshold([90, 85, 80], [20, 30, 40]) == 80
    assert ms.suggest_threshold([], [10]) is None


def test_evaluate_folder(app, fake_gemini, tmp_path, capsys):
    for group, a, b in (("same", RED, RED), ("same", BLUE, BLUE), ("different", RED, BLUE)):
        folder = tmp_path / group
        folder.mkdir(exist_ok=True)
        stem = f"{group}{len(list(folder.iterdir()))}"
        (folder / f"{stem}_a.jpg").write_bytes(jpeg_bytes(a))
        (folder / f"{stem}_b.jpg").write_bytes(jpeg_bytes(b))

    def compare(image_a, image_b):
        score = 90 if _same_color(_center_color(image_a), _center_color(image_b)) else 20
        return {
            "visual_similarity": score,
            "matching_features": [],
            "differences": [],
            "confidence": "high",
            "comparison_possible": True,
        }

    fake_gemini.compare_fn = compare
    result = ms.evaluate_folder(tmp_path)
    assert result["same"]["n"] == 2 and result["same"]["mean"] == 90
    assert result["different"]["n"] == 1 and result["different"]["median"] == 20
    assert result["suggested_threshold"] == 90
    out = capsys.readouterr().out
    assert "Suggested visual-similarity threshold" in out


def test_evaluate_folder_needs_gemini(app, tmp_path):
    assert ms.evaluate_folder(tmp_path) == {"error": "gemini_not_configured"}


def test_scan_result_rows_have_all_component_scores(app, user, fake_gemini, report_with_color):
    report_with_color(RED)
    ms.run_scan(user=user, mode="found", photo=scan_photo())
    row = db.session.scalars(sa.select(ScanResult)).one()
    for column in (
        "visual_score",
        "attribute_score",
        "distance_score",
        "recency_score",
        "final_score",
    ):
        assert getattr(row, column) is not None
    assert row.candidate_photo_id is not None
