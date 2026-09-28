"""User-facing copy kept in one place (SPEC 13).

Templates get these as ``S`` (e.g. ``{{ S.DISCLAIMER }}``); Python code
imports them directly. Keep the SPEC wording exactly where it is quoted.
"""

from __future__ import annotations

# SPEC 3 - must appear on every result screen.
DISCLAIMER = (
    "Similarity percentages are visual indicators only. They do not confirm that this is "
    "the same animal or establish ownership. Please verify with identifying details before "
    "arranging a handover."
)

# SPEC 5.2 - Found Pet Scanner with no relevant result.
NO_MATCH_FOUND_SCANNER = (
    "No similar lost-pet reports right now. This does not mean the animal isn't missing "
    "— the owner may not have reported yet."
)
NO_MATCH_LOST_SCANNER = (
    "No similar sightings right now. New sightings are checked against your report "
    "automatically, and we will alert you if a possible match appears."
)

# SPEC 3 - upload tips.
UPLOAD_TIPS = (
    "Use good lighting.",
    "Show the whole body.",
    "Make sure the face is clearly visible.",
    "One animal per photo.",
)
SCAN_TIP = "Make sure that pet is clearly visible for better results."
PHOTO_HINT = "Clear photos help more people identify your pet."

# SPEC 3 / 5.8
EMERGENCY_NOTE = (
    "PawBalik does not replace emergency services. For urgent situations involving people "
    "or dangerous animals, call 911 or your barangay hall."
)
SPECIES_NOTE = "For dogs and cats only"
SERVICE_AREA_NOTE = "PawBalik currently serves Tacloban City, Leyte only."
POSSIBLE_MATCH_WORDING = "possible match"  # never "match found" / "this is your pet"

# SPEC 3 - verification checklist shown inside conversations.
VERIFICATION_CHECKLIST = (
    "Ask about unique markings (scars, spots, eye colour, tail shape).",
    "Check the collar, tag or microchip if any.",
    "Ask for vet records or vaccination cards.",
    "Compare with older photos of the pet.",
    "Meet in a safe public place, e.g. your barangay hall.",
)

# SPEC 16.6
NONE_LOOK_RIGHT_TITLE = "None of these look right?"
NONE_LOOK_RIGHT_BODY = (
    "That's okay — not every found pet has a matching report yet. Submit the pet you "
    "scanned as a new sighting so owners searching their pet can find it."
)
REPORT_AS_SIGHTING = "Report as pet sightings"

# Scan progress stages (SPEC 16.5); {n} = candidate count.
SCAN_STAGES = (
    "Checking photo…",
    "Reading features…",
    "Comparing with {n} reports…",
    "Ranking…",
)

# Optional Filipino/Waray microcopy (SPEC 13).
MICROCOPY = {"lost": "Nawawala", "found": "Nakita"}

PRIVACY_CONSENT_LABEL = (
    "I have read the Privacy Notice and agree that PawBalik may process my personal data "
    "for reuniting lost pets and rescue coordination."
)
SIGHTING_CONSENT_LABEL = (
    "I agree that this sighting's location and my contact details may be shown to a pet "
    "owner whose search returns this sighting as a possible match."
)


def as_dict() -> dict[str, object]:
    """Every UPPER_CASE constant above, for the template context."""
    return {k: v for k, v in globals().items() if k.isupper()}
