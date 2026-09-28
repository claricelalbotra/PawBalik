# PawBalik JSON API

Generated from the Flask routes (`app/routes/*.py`). Pages (HTML) are listed in `docs/CONTRACTS.md`.

## Conventions

- **Base URL:** `http://127.0.0.1:5000` in development.
- **Auth:** session cookie from `/login`. Endpoints marked *login required* return `401` JSON when signed out.
- **CSRF:** every `POST`/`PATCH`/`DELETE` must send the header `X-CSRFToken: <token>`; the token is in `<meta name="csrf-token">` on every page (`main.js` `api()` does this for you). Missing/invalid token → `400 {"error": {"code": "csrf_failed", ...}}`.
- **Errors** always use one shape: `{"error": {"code": "string", "message": "human readable"}}` (validation errors may add `"fields": {...}`).
- **Status codes:** `200` OK, `201` created, `204` no content, `400` bad input/CSRF, `401` signed out, `403` forbidden, `404` not found *or not visible to you* (private sightings never reveal that they exist), `413` upload too large, `422` photo rejected (not a single dog/cat, not an image), `429` rate limit.
- **Times** are UTC ISO-8601 strings ending in `Z`.
- **Similarity** values are visual indicators only — they never confirm that two animals are the same pet.

## Lost Pet Reports

### `GET /api/lost-reports`

*Auth:* public.

GET /api/lost-reports?near=lat,lng&species=dog|cat&q=&status=lost&radius=&page=&per_page=

Public; hidden reports are never listed; no private contact data.

### `POST /api/lost-reports`

*Auth:* login required.

POST /api/lost-reports (multipart: fields + photos) -> 201 {"id", "case_number", "url"}.

### `GET /api/lost-reports/<int:report_id>`

*Auth:* public (owner/admin for hidden).

GET /api/lost-reports/<id> - public fields (404 if hidden, unless owner/admin).

### `GET /api/lost-reports/<int:report_id>/history`

*Auth:* login required.

GET /api/lost-reports/<id>/history -> {"history": [{from_status, to_status, changed_at, note}]}.

### `PATCH /api/lost-reports/<int:report_id>/status`

*Auth:* login required.

PATCH /api/lost-reports/<id>/status {"status": "found"|"closed", "note": str}.

## Scanner

### `POST /api/scans`

*Auth:* login required.

POST /api/scans (multipart: mode, photo, lat, lng, lost_report_id) -> 201 scan JSON.

### `GET /api/scans/<int:scan_id>`

*Auth:* login required.

GET /api/scans/<id> -> {"scan": {...}, "results": [...]} (scan owner or admin).

## Pet Sightings (private)

### `POST /api/sightings`

*Auth:* login required.

POST /api/sightings (multipart: fields + photos, and/or scan_id) -> 201 {"id", "url"}.

### `GET /api/sightings/<int:sighting_id>`

*Auth:* login required.

GET /api/sightings/<id> (same visibility rules as the page; logs non-finder access).

### `GET /api/sightings/mine`

*Auth:* login required.

GET /api/sightings/mine -> {"sightings": [...]} (the finder's own sightings).

## Messages

### `GET /api/conversations`

*Auth:* login required.

GET /api/conversations -> {"conversations": [...], "unread_total": n}.

### `POST /api/conversations`

*Auth:* login required.

POST /api/conversations {"lost_report_id", "sighting_id"?, "body"?} ->
201 {"id", "url", "created": true} (200 with "created": false when reused).

### `GET /api/conversations/<int:conversation_id>/messages`

*Auth:* login required.

GET /api/conversations/<id>/messages?after=<message_id> -> {"messages": [...]}.

Without ``after`` the latest messages are returned. The response also says
whether the viewer may still write (``can_send``) and the newest own message
the other person has read (``last_read_mine_id``, for "Seen" receipts).

### `POST /api/conversations/<int:conversation_id>/messages`

*Auth:* login required.

POST /api/conversations/<id>/messages - JSON {"type": "text"|"location_pin", "body",
"lat", "lng", "location_label"} or multipart with "photo" for images -> 201 message.

### `POST /api/conversations/<int:conversation_id>/read`

*Auth:* login required.

POST /api/conversations/<id>/read - mark the other participant's messages as read.

## Notifications & devices

### `POST /api/devices`

*Auth:* login required.

POST /api/devices {"token": "<fcm token>"} -> 201 (upsert device_tokens).

Clicking "Enable match alerts" is an explicit opt-in, so push_opt_in is switched on.

### `DELETE /api/devices/<path:token>`

*Auth:* login required.

DELETE /api/devices/<token> -> 204 (own tokens only).

### `GET /api/notifications`

*Auth:* login required.

GET /api/notifications?limit=20&before_id= -> {"notifications": [...], "unread": int}.

### `POST /api/notifications/<int:notification_id>/read`

*Auth:* login required.

POST /api/notifications/<id>/read -> {"ok": true, "unread": int}.

### `POST /api/notifications/read-all`

*Auth:* login required.

POST /api/notifications/read-all -> {"ok": true, "updated": int, "unread": 0}.

## Rescue

### `GET /api/providers`

*Auth:* public.

GET /api/providers?near=lat,lng&type=rescuer|vet|shelter&open_now=1 (or open=now|24_7).

### `POST /api/rescue-requests`

*Auth:* login required.

POST /api/rescue-requests {"lost_report_id"|"sighting_id", "provider_id"?, "note"?,
"include_phone"?} -> 201 {"id", "summary", "provider"}.

## Admin

### `GET /api/admin/stats`

*Auth:* admin.

GET /api/admin/stats -> {"active_lost_cases", "sightings", "scans_per_day": [...], "reunions", ...}.

## Examples

**Create a lost report**

```bash
curl -X POST http://127.0.0.1:5000/api/lost-reports -H "X-CSRFToken: $TOKEN" -b cookies.txt \
  -F species=dog -F pet_name=Bantay -F breed=Aspin -F colors=Brown -F size=medium -F sex=male \
  -F identifying_features='Red collar' -F lat=11.2445 -F lng=125.0036 -F place='Brgy. 37' \
  -F last_seen_date=2026-09-23 -F last_seen_time=17:30 -F photos=@dog.jpg
```

```json
{"id": 8, "case_number": "PB-2026-000008", "url": "/cases/8"}
```

**Found-pet scan** — `POST /api/scans` (multipart `mode=found`, `photo`, optional `lat`,`lng`) → `201`:

```json
{"scan_id": 5, "results": [{"candidate_type": "lost_report", "candidate_id": 8, "case_number": "PB-2026-000008",
  "final_score": 84.2, "tier": "high", "visual_score": 88, "attribute_score": 75.0, "distance_km": 0.6,
  "chip": "Lost 1 day ago", "detail_url": "/cases/8", "visual_check_unavailable": false,
  "explanation": {"matching_features": ["..."], "differences": ["..."]}}], "weak_count": 0, "warnings": []}
```

**Photo rejected** → `422` (other codes: `no_animal`, `multiple_animals`, `not_a_photo`, `photo_unreadable`, `invalid_image`):

```json
{"error": {"code": "not_dog_or_cat", "message": "This photo does not seem to show a dog or a cat. PawBalik works with dogs and cats only."}}
```
