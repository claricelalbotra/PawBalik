"""Philippine mobile number helpers (E.164: ``+639XXXXXXXXX``)."""

from __future__ import annotations

import re

_E164_PH = re.compile(r"^\+639\d{9}$")


def normalize_ph(raw: str) -> str:
    """Normalise a PH mobile number to E.164 (``+639171234567``).

    Accepts ``09171234567``, ``9171234567``, ``639171234567``,
    ``+63 917 123 4567``, ``(0917) 123-4567`` ... Raises ``ValueError`` for
    anything that is not a PH mobile number (landlines are rejected).
    """
    if raw is None:
        raise ValueError("Mobile number is required.")
    text = str(raw).strip()
    if not text:
        raise ValueError("Mobile number is required.")
    if re.search(r"[^\d\s\-\+\(\)\.]", text):
        raise ValueError("Mobile number may only contain digits, spaces, dashes and +.")
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)

    if has_plus:
        if not digits.startswith("63"):
            raise ValueError("Only Philippine mobile numbers (+63) are supported.")
        national = digits[2:]
    elif digits.startswith("63") and len(digits) == 12:
        national = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        national = digits[1:]
    else:
        national = digits

    if len(national) != 10 or not national.startswith("9"):
        raise ValueError("Enter a valid PH mobile number, e.g. 0917 123 4567.")
    return "+63" + national


def is_valid_e164_ph(value: str | None) -> bool:
    return bool(value and _E164_PH.match(value))


def mask_phone(e164: str | None) -> str:
    """``+639171234567`` -> ``+63 9** *** 4567`` (public display)."""
    if not e164:
        return ""
    if not is_valid_e164_ph(e164):
        return "*" * max(0, len(e164) - 4) + e164[-4:]
    return f"+63 9** *** {e164[-4:]}"


def format_phone(e164: str | None) -> str:
    """``+639171234567`` -> ``+63 917 123 4567`` (unmasked, for owners/admins)."""
    if not e164:
        return ""
    if not is_valid_e164_ph(e164):
        return e164
    n = e164[3:]
    return f"+63 {n[:3]} {n[3:6]} {n[6:]}"


def tel_href(e164: str | None) -> str:
    """Value for an ``<a href="tel:...">`` link."""
    return f"tel:{e164}" if e164 else ""
