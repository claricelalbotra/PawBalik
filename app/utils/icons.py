"""Inline SVG line icons for templates: ``{{ icon("paw") }}``.

Registered as a Jinja global in app/utils/filters.py. Icons are 24x24,
``stroke="currentColor"`` (they take the text colour), decorative by default
(``aria-hidden``); pass ``label="..."`` for a meaningful icon. Inline SVG
needs no extra requests and is allowed by the CSP.

Add icons to ICONS; the markup is static (never put user input here).
"""

from __future__ import annotations

from markupsafe import Markup, escape

_PAW = (
    '<g fill="currentColor" stroke="none"><circle cx="6.6" cy="10" r="1.9"/>'
    '<circle cx="9.6" cy="6.4" r="1.9"/><circle cx="14.4" cy="6.4" r="1.9"/>'
    '<circle cx="17.4" cy="10" r="1.9"/><path d="M12 11.4c-2.7 0-5 3-5 5 0 1.5 1.1 2.6 2.6 2.6'
    ' .9 0 1.5-.5 2.4-.5s1.5.5 2.4.5c1.5 0 2.6-1.1 2.6-2.6 0-2-2.3-5-5-5z"/></g>'
)
_SMALL_PAW = (
    '<g fill="currentColor" stroke="none"><circle cx="8.3" cy="9.6" r="1"/>'
    '<circle cx="10" cy="7.8" r="1"/><circle cx="12.2" cy="7.8" r="1"/><circle cx="13.9" cy="9.6" r="1"/>'
    '<path d="M11.1 10.4c-1.4 0-2.6 1.6-2.6 2.6 0 .8.6 1.3 1.3 1.3.5 0 .8-.3 1.3-.3s.8.3 1.3.3'
    'c.7 0 1.3-.5 1.3-1.3 0-1-1.2-2.6-2.6-2.6z"/></g>'
)

ICONS: dict[str, str] = {
    "paw": _PAW,
    "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/><path d="M10 20v-5.5h4V20"/>',
    "house-paw": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/>'
    + _SMALL_PAW.replace('cx="8.3"', 'cx="8.9"'),
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    "search-paw": '<circle cx="11" cy="11" r="7.2"/><path d="m20.5 20.5-4.3-4.3"/>' + _SMALL_PAW,
    "scan": '<path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16"/>'
    + _SMALL_PAW.replace("<g ", '<g transform="translate(.9 1.5)" '),
    "map-pin": '<path d="M12 21s-7-6.1-7-11.5a7 7 0 0 1 14 0C19 14.9 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
    "shield": '<path d="M12 3 19 6v5.2c0 4.6-3 8.3-7 9.8-4-1.5-7-5.2-7-9.8V6z"/><path d="m9 12 2 2 4-4"/>',
    "bell": '<path d="M6 16v-5a6 6 0 1 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "chat": '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H9l-5 4z"/>',
    "clipboard": '<rect x="5" y="4.5" width="14" height="16.5" rx="2"/><path d="M9 4.5V3h6v1.5"/><path d="M9 11h6M9 15h4"/>',
    "report": '<rect x="5" y="4.5" width="14" height="16.5" rx="2"/><path d="M9 4.5V3h6v1.5"/><path d="M12 10v6M9 13h6"/>',
    "rescue": '<path d="M12 20s-7.5-4.6-7.5-10.2A4.2 4.2 0 0 1 12 7.3a4.2 4.2 0 0 1 7.5 2.5C19.5 15.4 12 20 12 20z"/><path d="M12 10.5v5M9.5 13h5"/>',
    "heart": '<path d="M12 20s-7.5-4.6-7.5-10.2A4.2 4.2 0 0 1 12 7.3a4.2 4.2 0 0 1 7.5 2.5C19.5 15.4 12 20 12 20z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.2 3.6-7 8-7s8 2.8 8 7"/>',
    "community": '<circle cx="8" cy="8.5" r="3"/><circle cx="16" cy="8.5" r="3"/><path d="M2.5 20c0-3.3 2.5-6 5.5-6 1.6 0 3 .7 4 1.8 1-1.1 2.4-1.8 4-1.8 3 0 5.5 2.7 5.5 6"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "camera": '<path d="M4 8.5A1.5 1.5 0 0 1 5.5 7h2l1.7-2.5h5.6L16.5 7h2A1.5 1.5 0 0 1 20 8.5v9.5a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18z"/><circle cx="12" cy="13" r="3.5"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="m20.5 16.5-5-5-9 8"/>',
    "upload": '<path d="M12 15.5V4M7.5 8.5 12 4l4.5 4.5"/><path d="M4.5 15.5V19a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-3.5"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "check-circle": '<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.2 2.4 2.4 4.6-4.8"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "arrow-left": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "logout": '<path d="M9.5 4H5.5A1.5 1.5 0 0 0 4 5.5v13A1.5 1.5 0 0 0 5.5 20h4"/><path d="m15 8 4 4-4 4M19 12H9.5"/>',
    "settings": '<path d="M4 6.5h9M17 6.5h3M4 12h3M11 12h9M4 17.5h11M19 17.5h1"/><circle cx="15" cy="6.5" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17.5" r="2"/>',
    "admin": '<path d="M12 3 19 6v5.2c0 4.6-3 8.3-7 9.8-4-1.5-7-5.2-7-9.8V6z"/><circle cx="12" cy="10.5" r="2.2"/><path d="M8.3 16c.8-1.6 2.1-2.5 3.7-2.5s2.9.9 3.7 2.5"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
    "warning": '<path d="M10.3 4.6 2.8 18a2 2 0 0 0 1.7 3h15a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/><path d="M12 10v4M12 17.2h.01"/>',
    "question": '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.5a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .9-1 1.7M12 16.8h.01"/>',
    "phone": '<path d="M5.5 4H9l1.8 4.6-2.3 1.5a10.5 10.5 0 0 0 5.4 5.4l1.5-2.3L20 15v3.5a1.5 1.5 0 0 1-1.6 1.5C10.7 19.5 4.5 13.3 4 5.6A1.5 1.5 0 0 1 5.5 4z"/>',
    "mail": '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="m4 7 8 6 8-6"/>',
    "directions": '<path d="M12 2.8 21.2 12 12 21.2 2.8 12z"/><path d="M9.5 14.5v-2a1 1 0 0 1 1-1H15m-2-2 2 2-2 2"/>',
    "external": '<path d="M14 4h6v6M20 4l-8.5 8.5"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
    "crosshair": '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    "send": '<path d="M4 12 20 4l-4.5 16-3.5-6.5z"/><path d="m12 13.5 8-9.5"/>',
    "gift": '<rect x="3.5" y="9" width="17" height="11.5" rx="1.5"/><path d="M3.5 13h17M12 9v11.5M12 9C10 6 6.5 5.2 6.5 7.3S10 9 12 9zm0 0c2-3 5.5-3.8 5.5-1.7S14 9 12 9z"/>',
    "flag": '<path d="M5.5 21V4M5.5 4.5h11l-2 4 2 4h-11"/>',
    "lock": '<rect x="5" y="11" width="14" height="9.5" rx="2"/><path d="M8.5 11V7.5a3.5 3.5 0 0 1 7 0V11"/>',
    "eye-off": '<path d="M3 3l18 18M10.6 5.2A9.5 9.5 0 0 1 12 5c5 0 8.5 4.5 9.5 7a13 13 0 0 1-2.7 3.9M6.5 6.6C4.4 8 3 10.2 2.5 12c1 2.5 4.5 7 9.5 7 1.8 0 3.4-.6 4.8-1.4"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "edit": '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "trash": '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    "filter": '<path d="M4 5h16l-6 7.5V19l-4 1.5v-8z"/>',
    "copy": '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="1.8"/><path d="M15.5 8.5V5.5A1.5 1.5 0 0 0 14 4H5.5A1.5 1.5 0 0 0 4 5.5V14a1.5 1.5 0 0 0 1.5 1.5h3"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "dog": '<path d="M7.6 5.2C4.6 4.6 3 7 3.5 10.5c.3 1.9 1.4 2.9 2.4 2.4"/><path d="M16.4 5.2c3-.6 4.6 1.8 4.1 5.3-.3 1.9-1.4 2.9-2.4 2.4"/><path d="M7 7.4C8 5.8 10 5 12 5s4 .8 5 2.4c1.1 1.9 1.1 5.1 0 7.6C16 17.4 14 19.5 12 19.5S8 17.4 7 15c-1.1-2.5-1.1-5.7 0-7.6z"/><path d="M10 11h.01M14 11h.01"/><path d="M11 14.4h2l-1 1.1z" fill="currentColor"/>',
    "cat": '<path d="M5 4.5 8.2 8.5h7.6L19 4.5v8.7c0 4-3.1 6.8-7 6.8s-7-2.8-7-6.8z"/><path d="M9.5 12.5h.01M14.5 12.5h.01"/><path d="M11 15h2l-1 1.1z" fill="currentColor"/><path d="M3 14.5h3.5M3.5 17l3-1M21 14.5h-3.5M20.5 17l-3-1"/>',
    "sparkle": '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z"/>',
}


def icon(name: str, class_: str = "", label: str | None = None, size: int | None = None) -> Markup:
    """Render ``<svg class="icon ...">`` for ``name`` (unknown names render the paw)."""
    body = ICONS.get(name, _PAW)
    classes = "icon" + (f" {escape(class_)}" if class_ else "")
    size_attr = f' width="{int(size)}" height="{int(size)}"' if size else ""
    a11y = (
        f' role="img" aria-label="{escape(label)}"'
        if label
        else ' aria-hidden="true" focusable="false"'
    )
    return Markup(
        f'<svg class="{classes}" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        f' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"{size_attr}{a11y}>'
        f"{body}</svg>"
    )
