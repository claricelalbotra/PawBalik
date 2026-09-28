# PawBalik: Developer Contracts (binding)

Written by the foundation step. These names are **final**. Feature teams build behind
them; nobody renames an endpoint, model field, fixture, helper or CSS class listed here.
If you need a change to a shared file (see "Ownership" below), ask the integrator.
`docs/SPEC.md` stays the source of truth for behaviour; this file pins the names.

---

## 1. Ownership

| Team | Files |
|---|---|
| **shared** (foundation / integrator / stabilizer) | `app/__init__.py`, `app/config.py`, `app/extensions.py`, `app/models/**`, `app/utils/**`, `app/routes/auth.py`, `app/routes/main.py`, `app/services/{job,policy,audit}_service.py`, `app/templates/{base,index,login,register,profile,about,privacy}.html`, `app/templates/partials/**`, `app/templates/errors/**`, `app/static/css/style.css`, `app/static/js/{main,map}.js`, `app/static/images/**` (except `samples/`), `database/schema.sql`, `run.py`, `tests/conftest.py`, `tests/test_{utils,policies,jobs,auth,security,app,schema}*.py`, `requirements.txt`, `pytest.ini`, `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`, `docs/**`, `uploads/` |
| **engine** | `app/services/{gemini,matching,storage}_service.py`, `tests/test_{storage,gemini,matching}*.py` |
| **reports** | `app/routes/{pets,cases}.py`, `app/templates/lost_pet/**`, `app/templates/dashboard.html`, `app/static/js/lost_pet.js`, `app/static/css/_parts/reports.css`, `tests/test_{pets,cases}*.py` |
| **scanner_sightings** | `app/routes/{scanner,sightings}.py`, `app/templates/{scanner,sightings}/**`, `app/static/js/scanner.js`, `app/static/css/_parts/scanner.css`, `tests/test_{scanner,sightings}*.py` |
| **messages** | `app/routes/messages.py`, `app/services/message_service.py`, `app/templates/messages/**`, `app/static/js/messages.js`, `app/static/css/_parts/messages.css`, `tests/test_messages*.py` |
| **notifications** | `app/routes/notifications.py`, `app/services/{notification,sms}_service.py`, `app/templates/notifications/**`, `app/static/js/notifications.js`, `app/static/css/_parts/notifications.css`, `tests/test_{notifications,sms,push,otp}*.py` |
| **rescue_admin** | `app/routes/{rescue,admin}.py`, `app/templates/{rescue,admin}/**`, `app/static/js/{rescue,admin}.js`, `app/static/css/_parts/rescue_admin.css`, `app/static/images/samples/**`, `database/seed.py`, `tests/test_{rescue,admin,seed}*.py` |

Team files currently hold **stubs**: the routes exist with their final names, URLs and
login decorators and render `errors/coming_soon.html` (pages) or a 501 JSON error (APIs).
Some services have minimal working versions so other teams are not blocked
(storage: local only; notify: in-app record; message_service: conversation helpers;
seed: admin account; job handlers: logged no-ops). Replace them. Keep the signatures.

**Never:** add top-level folders or packages (no `blueprints/`, `migrations/`, `scripts/`,
`cli/`, `worker.py`), `pip install` anything, edit `.env`, commit, or call a paid API in tests.

---

## 2. Endpoints (blueprint = file name; every module exposes `bp`)

One blueprint per file in `app/routes/`, registered by `app.ROUTE_MODULES`. API rules
live in the same blueprint with an `/api/...` URL. **Anything under `/api/` answers with
JSON errors** `{"error": {"code": "...", "message": "..."}}` (optional `"details"`).
Guards: page routes use `@login_required` (guests are redirected to `auth.login?next=`)
or `@admin_required`; API routes use `@api_login_required` / `@api_admin_required` (401/403 JSON).

### main (shared, complete)
| Endpoint | Method | URL |
|---|---|---|
| `main.index` | GET | `/` |
| `main.about` | GET | `/about` |
| `main.privacy` | GET | `/privacy` |
| `main.healthz` | GET | `/healthz` |
| `main.media` | GET | `/media/<path:key>` (`?v=thumb`); local photos after `policy_service.can_view_photo`, else 404 |

The app-wide error handlers are also in `main.py`.

### auth (shared, complete)
| `auth.register` | GET, POST | `/register` (then redirects to `notifications.verify_phone`) |
|---|---|---|
| `auth.login` | GET, POST | `/login?next=` (POST rate-limited `RATE_LIMIT_LOGIN`) |
| `auth.logout` | **POST** | `/logout` (form with `csrf_token`) |
| `auth.profile` | GET, POST | `/profile` (name, email, mobile, `sms_opt_in`, `push_opt_in`) |
| `auth.change_password` | POST | `/profile/password` (fields prefixed `pw-`) |

### pets (reports)
| `pets.browse` | GET | `/lost-pets` (public; `?species=&near=lat,lng&status=`) |
|---|---|---|
| `pets.new` | GET, POST | `/lost-pets/new` (login; 5-step wizard, `lost_pet/create_report.html`) |
| `pets.edit` | GET, POST | `/lost-pets/<int:report_id>/edit` (login; owner/admin) |
| `pets.api_list` | GET | `/api/lost-reports?near=lat,lng&species=` (public) |
| `pets.api_create` | POST | `/api/lost-reports` (login) -> 201 `{"id","case_number","url"}` |
| `pets.api_detail` | GET | `/api/lost-reports/<int:report_id>` (public unless hidden) |

### cases (reports)
| `cases.dashboard` | GET | `/my-cases` (login; `dashboard.html`) |
|---|---|---|
| `cases.detail` | GET | `/cases/<int:report_id>` (public unless hidden; `lost_pet/details.html`) |
| `cases.by_number` | GET | `/cases/<case_number>` (e.g. `/cases/PB-2026-000123`) |
| `cases.change_status` | POST | `/cases/<int:report_id>/status` (login; fields `status`, `note`) |
| `cases.api_update_status` | PATCH | `/api/lost-reports/<int:report_id>/status` `{"status","note"}` |
| `cases.api_history` | GET | `/api/lost-reports/<int:report_id>/history` |

### scanner (scanner_sightings)
| `scanner.index` | GET | `/scan` (login; bottom-nav "Scan") |
|---|---|---|
| `scanner.found` | GET | `/scan/found` (login; "Match a Found Pet", `scanner/choose.html`) |
| `scanner.lost` | GET | `/scan/lost` (login; `?lost_report_id=`) |
| `scanner.submit` | POST | `/scan` (login; no-JS fallback) |
| `scanner.results` | GET | `/scans/<int:scan_id>` (login; `?weak=1`) |
| `scanner.api_create` | POST | `/api/scans` (login; rate limit `RATE_LIMIT_SCANS`) |
| `scanner.api_detail` | GET | `/api/scans/<int:scan_id>` |

### sightings (scanner_sightings)
| `sightings.new` | GET, POST | `/sightings/new` (login; `?scan_id=` pre-fills the scanned photo) |
|---|---|---|
| `sightings.detail` | GET | `/sightings/<int:sighting_id>` (login; `can_view_sighting`) |
| `sightings.update_status` | POST | `/sightings/<int:sighting_id>/status` |
| `sightings.api_create` | POST | `/api/sightings` |
| `sightings.api_mine` | GET | `/api/sightings/mine` |
| `sightings.api_detail` | GET | `/api/sightings/<int:sighting_id>` |

`app/routes/sightings.py` also defines `expire_sightings()` and registers job `expire_sightings`.

### messages (messages)
| `messages.inbox` | GET | `/messages` |
|---|---|---|
| `messages.new` | GET, POST | `/messages/new?lost_report_id=&sighting_id=` ("Message owner/finder" buttons link here) |
| `messages.thread` | GET | `/messages/<int:conversation_id>` |
| `messages.report_user` | POST | `/messages/<int:conversation_id>/report` (field `reason`) |
| `messages.api_list` | GET | `/api/conversations` |
| `messages.api_create` | POST | `/api/conversations` `{"lost_report_id","sighting_id"?}` |
| `messages.api_messages` | GET | `/api/conversations/<int:conversation_id>/messages?after=<id>` |
| `messages.api_send` | POST | `/api/conversations/<int:conversation_id>/messages` |
| `messages.api_mark_read` | POST | `/api/conversations/<int:conversation_id>/read` |

### rescue (rescue_admin)
| `rescue.directory` | GET | `/rescue` (public; `?near=&type=&open=24_7`) |
|---|---|---|
| `rescue.provider` | GET | `/rescue/providers/<int:provider_id>` |
| `rescue.request_help` | GET, POST | `/rescue/request?lost_report_id=` or `?sighting_id=` (login) |
| `rescue.api_providers` | GET | `/api/providers?near=lat,lng&type=` |
| `rescue.api_create_request` | POST | `/api/rescue-requests` |

### notifications (notifications)
| `notifications.index` | GET | `/notifications` |
|---|---|---|
| `notifications.open` | GET | `/notifications/<int:notification_id>` (mark read + redirect) |
| `notifications.mark_all_read` | POST | `/notifications/read-all` |
| `notifications.verify_phone` | GET, POST | `/account/verify-phone` |
| `notifications.service_worker` | GET | `/firebase-messaging-sw.js` (site root) |
| `notifications.api_list` | GET | `/api/notifications` |
| `notifications.api_mark_read` | POST | `/api/notifications/<int:notification_id>/read` |
| `notifications.api_register_device` | POST | `/api/devices` `{"token"}` |
| `notifications.api_delete_device` | DELETE | `/api/devices/<path:token>` |

Partial for other pages: `notifications/_enable_alerts.html` ("Enable match alerts").

### admin (rescue_admin; all `@admin_required`)
`admin.dashboard` GET `/admin` · `admin.providers` GET `/admin/providers` ·
`admin.provider_new` GET/POST `/admin/providers/new` · `admin.provider_edit` GET/POST
`/admin/providers/<int:provider_id>/edit` · `admin.provider_delete` POST
`/admin/providers/<int:provider_id>/delete` · `admin.reports` GET `/admin/reports` ·
`admin.report_visibility` POST `/admin/reports/<int:report_id>/visibility` (`action=hide|unhide`, `reason`) ·
`admin.sightings` GET `/admin/sightings` · `admin.sighting_visibility` POST
`/admin/sightings/<int:sighting_id>/visibility` · `admin.users` GET `/admin/users` ·
`admin.user_active` POST `/admin/users/<int:user_id>/active` (`active=0|1`) ·
`admin.abuse_reports` GET `/admin/abuse-reports` · `admin.abuse_report_update` POST
`/admin/abuse-reports/<int:abuse_report_id>` · `admin.logs` GET `/admin/logs` ·
`admin.api_stats` GET `/api/admin/stats`.

### JSON error codes in use
`bad_request` 400 · `csrf_failed` 400 · `validation_error` 400/422 · `auth_required` 401 ·
`forbidden` 403 · `not_found` 404 · `method_not_allowed` 405 · `file_too_large` 413 ·
`rate_limited` 429 · `server_error` 500 · `not_implemented` 501 · scan rejections use
`ScanRejected.code` with 422 (`not_dog_or_cat`, `multiple_animals`, `not_a_photo`, ...).
Build them with `app.utils.http.json_error(code, message, status=400, details=None)`.

---

## 3. Models (`from app.models import ...`)

All tables follow SPEC section 7, InnoDB/utf8mb4_unicode_ci, and work on SQLite,
MariaDB 10.4 and MySQL 8. **Datetimes are naive UTC** (`app.utils.time.utcnow()`); show
them with the Jinja filters, which convert to Asia/Manila. Enum columns store plain strings;
compare with the constants in `app.models.enums` (e.g. `ReportStatus.LOST == "lost"`).
After changing a model: `python run.py export-schema` (tests fail if `database/schema.sql`
is stale). Changing models is shared-only: ask the integrator.

- `abuse_reports` -> **AbuseReport**: id, reporter_id, target_user_id, conversation_id, reason, status(open|reviewed|dismissed), created_at. Relationships: reporter, target_user, conversation
- `assistance_providers` -> **AssistanceProvider**: id, name, type(rescuer|vet|shelter), address, barangay, lat, lng, phones (comma-separated; `.phone_list`), facebook_url, hours_text, is_24_7, services, is_active, is_sample, created_at, updated_at
- `audit_logs` -> **AuditLog**: id, actor_id, event, target_type, target_id, ip, details (JSON), created_at. Write via `audit_service.log_event` only
- `case_counters` -> **CaseCounter**: year (PK), last_value. Use `next_case_number()` only
- `case_status_history` -> **CaseStatusHistory**: id, lost_report_id, from_status, to_status, changed_by, note, changed_at. Relationships: lost_report, actor
- `conversations` -> **Conversation**: id, lost_report_id, sighting_id, owner_id, other_user_id, created_at, last_message_at; unique (lost_report_id, other_user_id). Relationships: lost_report, sighting, owner, other_user, messages. Helpers: `participant_ids`, `is_participant(user)`, `other_participant(user)`, `unread_count_for(user)`
- `device_tokens` -> **DeviceToken**: id, user_id, fcm_token (unique), user_agent, last_seen_at, created_at
- `image_comparisons` -> **ImageComparison**: id, query_sha256, candidate_photo_id, model_name, result (JSON), created_at; unique triple
- `jobs` -> **Job**: id, type, payload (JSON), status(pending|running|done|failed), attempts, last_error, run_after, locked_at, created_at, updated_at. Use `job_service.enqueue` only
- `lost_reports` -> **LostReport**: id, case_number (unique), owner_id, pet_name, species(dog|cat), breed, colors, size(small|medium|large), sex(male|female|unknown), age_text, identifying_features, last_seen_lat, last_seen_lng, last_seen_place, last_seen_at, contact_name, contact_email, contact_phone (E.164), show_phone, reward_amount (Numeric 10,2), reward_text, status(lost|found|closed), is_hidden, hidden_reason, hidden_by, hidden_at, ai_attributes (JSON), created_at, updated_at. Relationships: owner, hidden_by_user, status_history. Helpers: `photos`, `primary_photo`, `is_public`, `is_searchable`, `has_location`, `can_transition_to(s)`, `change_status(to, actor=, note=)` (raises `InvalidStatusTransition`), `record_created(actor=)`
- `messages` -> **Message**: id, conversation_id, sender_id (None = system), type(text|location_pin|image|system), body, lat, lng, location_label, photo_id, read_at, created_at
- `notifications` -> **Notification**: id, user_id, type (event name), title, body, link_url (relative), is_read, created_at
- `phone_verifications` -> **PhoneVerification**: id, user_id, mobile_e164, code_hash, expires_at, attempts, consumed_at, created_at
- `photos` -> **Photo**: id, owner_type(lost_report|sighting|scan|message), owner_id, storage(cloudinary|local), public_id (unique; local = key under UPLOAD_FOLDER), secure_url, width, height, sha256 (of original bytes), is_primary, sort_order, ai_attributes, created_at. `owner_id` meaning: lost_report -> lost_reports.id, sighting -> sightings.id, scan -> scans.id (None until attached with `attach_to`), **message -> conversations.id**. Helpers: `Photo.for_owner(type, id)`, `Photo.primary_for(type, id)`, `Photo.by_key(key)`
- `push_log` -> **PushLog**: id, user_id, token_id, title, status (sent|failed|skipped), error, created_at
- `rescue_requests` -> **RescueRequest**: id, user_id, lost_report_id, sighting_id, provider_id, summary_text, created_at
- `scan_results` -> **ScanResult**: id, scan_id, candidate_type(lost_report|sighting), candidate_id, candidate_photo_id, visual_score, attribute_score, distance_km, distance_score, recency_score, final_score, tier(high|moderate|low|hidden), rank, visual_check_unavailable, explanation (JSON `{matching_features, differences, confidence}`), notified_at, created_at; unique (scan_id, candidate_type, candidate_id). Helpers: `candidate`, `similarity_percent`
- `scans` -> **Scan**: id, user_id, mode(found|lost), trigger(manual|background), query_photo_id, source_lost_report_id, source_sighting_id, scan_lat, scan_lng, precheck (JSON), attributes (JSON), status(pending|running|complete|rejected|failed), error, result_count, gemini_ok, completed_at, created_at. Relationships: results (ordered by rank). Background scans: `user_id` = the owner being notified
- `sighting_access_log` -> **SightingAccessLog**: id, sighting_id, viewer_id, scan_id, accessed_at
- `sightings` -> **Sighting**: id, finder_id, species, breed, colors, size, condition(healthy|injured|unknown), is_holding_animal, notes, seen_lat, seen_lng, seen_place, seen_at, contact_name, contact_email, contact_phone, contact_pref(in_app|phone), status(active|matched|expired), is_hidden, hidden_reason, hidden_by, hidden_at, ai_attributes, expires_at, created_at, updated_at. Helpers: `photos`, `primary_photo`, `is_searchable`, `shares_phone`, `set_default_expiry(days)`
- `sms_log` -> **SmsLog**: id, user_id, to_number, message, purpose (otp|possible_match), provider, provider_message_id, status (sent|failed|skipped), error, created_at
- `users` -> **User**: id, full_name, email (unique, lower-case), mobile_e164 (unique), mobile_verified_at, password_hash (argon2), role(user|admin), is_active, sms_opt_in, push_opt_in, last_login_at, privacy_consented_at, created_at, updated_at. Helpers: `set_password`, `check_password`, `is_admin`, `first_name`, `mobile_verified`, `can_receive_sms`, `set_mobile(e164)` (clears verification)

Enum classes (values, `.label(v)`, `.choices()`): `UserRole`, `Species`, `PetSize`, `PetSex`,
`ReportStatus` (+`TRANSITIONS`), `SightingStatus`, `AnimalCondition`, `ContactPref`,
`PhotoOwner`, `StorageKind`, `ScanMode`, `ScanTrigger`, `ScanStatus`, `CandidateType`,
`MatchTier`, `MessageType`, `ProviderType`, `AbuseStatus`, `JobStatus`, `NotificationEvent`,
`SmsPurpose`. Pure functions: `allowed_transitions(status)`, `can_transition(a, b)`.

---

## 4. Services and helpers

### Shared (complete)
- `policy_service`: `is_admin(u)`, `is_logged_in(u)`, `can_manage_report(u, r)`, `can_view_report(u, r)`, `can_view_report_phone(u, r)`, `can_message_owner(u, r)`, `can_view_sighting(u, s)`, `can_manage_sighting(u, s)`, `visible_sighting_ids_via_scans(u)` (a SELECT), `can_view_scan(u, scan)`, `can_view_conversation(u, c)` (participants only; admins excluded), `can_post_in_conversation(u, c)`, `can_view_photo(u, p)`, `can_request_rescue(u, report=, sighting=)`. A sighting is visible to its finder, admins, an owner holding a ScanResult for it (manual or background scan), or the other participant of a conversation the finder attached it to; hidden sightings only to finder/admin.
- `audit_service.log_event(event, *, actor=None, target=None, details=None, commit=False)`; `log_sighting_access(sighting, viewer, scan=None)`. Events are dotted: `auth.login_failed`, `sighting.access`, `admin.report_hidden`, ...
- `job_service.enqueue(job_type, payload, run_after=None) -> Job`, `@register(job_type)`, `run_pending(limit=10)`, `run_worker(once=False)`, `HANDLER_MODULES`, `JOB_TYPES`. Handlers take `payload: dict`, may commit, must be idempotent. Payloads: `match_lost_report {"lost_report_id"}`, `match_sighting {"sighting_id"}`, `extract_attributes {"photo_id"}`, `dispatch_notification {"notification_id", "event"}`, `expire_sightings {}`, `cleanup_scan_photos {}`. Enqueue then commit.
- `app.utils.case_numbers.next_case_number(year=None) -> "PB-YYYY-NNNNNN"` (inside the saving transaction), `parse_case_number`.
- `app.utils.geo`: `haversine_km`, `in_service_area`, `service_area_bounds`, `distance_or_none`, `parse_near("lat,lng")`.
- `app.utils.maps`: `view_url(lat, lng)`, `directions_url(lat, lng, mode="driving")`.
- `app.utils.phone`: `normalize_ph(raw)` (raises `ValueError`), `mask_phone`, `format_phone`, `tel_href`, `is_valid_e164_ph`.
- `app.utils.validators`: `ValidationError(message, field)`, `password_problems`, `clean_text`, `parse_choice`, `parse_coordinates(lat, lng, require_service_area=True)`, `parse_local_datetime(date, time)` (Manila -> naive UTC), `parse_bool`, `parse_int`.
- `app.utils.http`: `json_error`, `json_ok`, `wants_json`, `is_safe_redirect_url`, `safe_next_url(default)`, `coming_soon(...)`, `api_not_implemented(feature)`.
- `app.utils.auth`: `admin_required`, `api_login_required`, `api_admin_required`.
- `app.utils.time`: `utcnow()` (naive UTC; **use this, never `datetime.now()`**), `to_local`, `local_now`, `local_to_utc`, `combine_local`, `days_ago`, `start_of_local_day_utc`.
- `app.utils.cards`: `lost_report_card(report, distance_km=, similarity=, tier=, explanation=, ...)`, `sighting_card(...)` -> dicts for the `pet_card` macro.
- `app.utils.strings`: UI copy (`DISCLAIMER` exact SPEC 3 text, `NO_MATCH_FOUND_SCANNER`, `UPLOAD_TIPS`, `VERIFICATION_CHECKLIST`, `EMERGENCY_NOTE`, `NONE_LOOK_RIGHT_*`, `SCAN_STAGES`, consent labels). In templates: `S.DISCLAIMER`.
- `app.extensions`: `db`, `login_manager`, `csrf`, `limiter`, `config_limit("RATE_LIMIT_X")` for `@limiter.limit(config_limit("RATE_LIMIT_SCANS"), methods=["POST"])`.

### Team services (signatures final)
- **storage_service** (engine): `save_image(file, *, owner_type, owner_id=None, folder, is_primary=False, sort_order=0) -> Photo` (validates with Pillow, strips EXIF, sha256 of original, flushes; raises `ImageValidationError(message, code)`), `default_folder(owner_type, owner_id=None)`, `photo_url(photo, variant="full"|"thumb")`, `image_bytes_for_ai(photo)` (JPEG <= 1024 px), `delete_photo(photo)`, `local_path(key, variant="full")`, `backend()`. Local version works now; Cloudinary is TODO.
- **gemini_service** (engine): `GeminiService().is_configured()`, `.precheck(bytes)`, `.extract_attributes(bytes)`, `.compare(a, b)` (shapes in the docstring / SPEC 6.2, 6.3, 6.5); `GeminiError`, `GeminiNotConfigured`, `get_gemini()`.
- **matching_service** (engine): `run_scan(*, user, mode, photo, lat=None, lng=None, source_lost_report=None, source_sighting=None, trigger="manual") -> Scan`, `ScanRejected(code, message, details)`, `results_for_display(scan, include_weak=False) -> list[dict]` (card dict keys + `result_id`, `candidate_type`, `candidate_id`, `final_score`, `visual_check_unavailable`), `get_candidate_pool(mode, user, **filters)`, `match_new_lost_report(report_id)`, `match_new_sighting(sighting_id)`, `extract_attributes_for_photo(photo_id)`, `cleanup_scan_photos()`, `cli_match_test(photo_path, mode, lat, lng)`, `evaluate_folder(folder)`.
- **notification_service** (notifications): `notify(user, event, data) -> Notification` (in-app row now; push/SMS dispatch via job), `unread_count(user)`, `dispatch(notification_id, event)`. Events and data keys: `possible_match {scan_id, lost_report_id, sighting_id?, similarity, case_number?, link_url}`, `new_message {conversation_id, message_id?, sender_name?, link_url}`, `case_status_changed {lost_report_id, case_number?, from_status, to_status, link_url}`; optional `title`/`body` overrides.
- **sms_service** (notifications): `SmsResult(ok, provider, message_id, error)`, `SmsProvider.send(to_e164, message)`, `ConsoleSmsProvider`, `MemorySmsProvider` (appends `{"to","message"}` to `current_app.extensions["sms_outbox"]`), `BulkSmsPhProvider` (TODO(verify)), `get_provider()`, `send_sms(user, message, purpose)`, `send_otp(user)`, `verify_otp(user, code) -> bool`. Push memory provider must append dicts to `current_app.extensions["push_outbox"]`.
- **message_service** (messages): `get_or_create_conversation(lost_report, other_user, sighting=None)` (raises `ConversationError`), `post_system_message(conversation, body)`, `conversations_for_report(report)`, `send_message(conversation, sender, *, type="text", body=None, lat=None, lng=None, location_label=None, photo=None)`.
- **database/seed.py** (rescue_admin): `run_seed(reset_demo=False) -> dict`, `ensure_admin()`.

---

## 5. Config keys (`current_app.config[...]`, all in `app/config.py` and `.env.example`)

Core: `SECRET_KEY`, `APP_BASE_URL`, `APP_NAME`, `APP_TAGLINE`, `APP_TIMEZONE`, `RUN_HOST`, `RUN_PORT`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SQLALCHEMY_DATABASE_URI` (env `DATABASE_URL`).
Area/map: `SERVICE_AREA_NAME`, `SERVICE_AREA_BOUNDS` {south 11.15, west 124.88, north 11.40, east 125.08}, `SERVICE_AREA_CENTER`, `MAP_DEFAULT_ZOOM`, `OUT_OF_AREA_POLICY` (reject|warn), `MAP_PICKER` (leaflet|none), `LEAFLET_*`, `OSM_TILE_URL`, `OSM_ATTRIBUTION`.
Uploads: `STORAGE_BACKEND` (auto|cloudinary|local), `CLOUDINARY_URL`, `CLOUDINARY_FOLDER_ROOT`, `UPLOAD_FOLDER` (default `<root>/uploads/pets`), `MAX_UPLOAD_BYTES` (8 MB), `MAX_PHOTOS_PER_REPORT` (5), `MAX_PHOTOS_PER_SIGHTING` (5), `ALLOWED_IMAGE_FORMATS`, `MAX_CONTENT_LENGTH`, `AI_IMAGE_MAX_EDGE` (1024), `THUMB_SIZE` (400), `CLIENT_COMPRESS_MAX_EDGE` (1600), `SCAN_PHOTO_RETENTION_DAYS` (30).
Gemini: `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`, engine verifies), `GEMINI_TEMPERATURE`, `GEMINI_TIMEOUT_SECONDS`, `GEMINI_MAX_ATTEMPTS`, `GEMINI_MAX_WORKERS`.
Matching: `MATCH_WEIGHTS` {visual .70, attribute .15, distance .10, recency .05}, `MATCH_RADIUS_KM` 15, `MATCH_DATE_WINDOW_DAYS` 60, `MATCH_TOP_N` 15, `MATCH_TIERS` [(high, 80, label), (moderate, 60, ...), (low, 40, ...)], `MATCH_HIDDEN_LABEL`, `MATCH_RELEVANCE_THRESHOLD` 40, `MATCH_NOTIFY_THRESHOLD` 75, `SIGHTING_EXPIRY_DAYS` 30.
Jobs: `JOBS_EAGER`, `JOBS_EAGER_RAISE`, `JOBS_MAX_ATTEMPTS`, `JOBS_BACKOFF_SECONDS`, `JOBS_LOCK_TIMEOUT_SECONDS`, `WORKER_POLL_SECONDS`, `PERIODIC_JOBS`.
Notifications: `SMS_PROVIDER` (console|bulksms|memory), `SMS_DAILY_MATCH_LIMIT` 5, `SMS_MAX_CHARS`, `BULKSMS_API_URL`, `BULKSMS_API_KEY`, `BULKSMS_SENDER_NAME`, `BULKSMS_TIMEOUT_SECONDS`, `OTP_LENGTH`, `OTP_TTL_MINUTES`, `OTP_MAX_ATTEMPTS`, `OTP_RESEND_COOLDOWN_SECONDS`, `PUSH_PROVIDER` (fcm|memory), `FIREBASE_CREDENTIALS_PATH`, `FIREBASE_WEB_CONFIG`, `FIREBASE_VAPID_KEY`, `FIREBASE_JS_VERSION`.
Messaging: `MESSAGE_POLL_SECONDS` 4, `MESSAGE_MAX_LENGTH` 2000.
Rate limits: `RATELIMIT_ENABLED`, `RATE_LIMIT_LOGIN`, `_REGISTER`, `_SCANS`, `_REPORTS`, `_SIGHTINGS`, `_MESSAGES`, `_OTP`, `_RESCUE_REQUESTS`, `_ABUSE_REPORTS`.

Testing config: SQLite in memory (StaticPool), `JOBS_EAGER=True`, `JOBS_EAGER_RAISE=True`, CSRF off, rate limiting off, `STORAGE_BACKEND=local`, `GEMINI_API_KEY=None`, `SMS_PROVIDER=PUSH_PROVIDER="memory"`.

---

## 6. Test fixtures (`tests/conftest.py`)

`app` (fresh app + DB per test; override config with `@pytest.mark.config(KEY=value)`),
`client`, `db_session`, `csrf_client` (CSRF on) + helper `get_csrf_token(client, path="/")`
(`from conftest import get_csrf_token`) or fixture `csrf_token`,
`make_user(**fields)`, `make_admin(**fields)`, `user`, `admin`, `login(client, user)`,
`make_photo(owner_type, owner_id, color=, is_primary=, sort_order=, ai_attributes=, commit=)`,
`make_lost_report(owner=None, photos=1, **fields)`, `make_sighting(finder=None, photos=1, **fields)`,
`make_scan_result(user, candidate, final_score=82.0, trigger="manual")`,
`jpeg` / `jpeg_bytes(color, size)`, `upload` / `upload_file(name, color, size)` (for multipart posts),
`fake_gemini` (attributes `precheck_result`, `attributes_result`, `compare_score`, `compare_fn`,
`fail` set, `configured`, `calls`), `sms_outbox`, `push_outbox`, `run_jobs()`.
Constants: `DEFAULT_PASSWORD = "Tacloban2026"`, `TACLOBAN = (11.2437, 125.0046)`.
Probe tests go in `tests/_probes/<label>/` (not collected) and must be deleted.

---

## 7. Templates, macros and globals

Every page: `{% extends "base.html" %}` with blocks `title`, `description`, `head`,
`body_class`, `content`, `scripts`. Wrap content in `<div class="container">`.
Page scripts: `{% block scripts %}<script type="module" src="{{ url_for('static', filename='js/scanner.js') }}"></script>{% endblock %}`.
**No inline `<script>`** (CSP). Pass data with `data-*` attributes or
`<script type="application/json" id="...">{{ data|tojson }}</script>`.
Header highlighting: `nav_active` is derived from the blueprint (`report`, `match`, `rescue`,
`about`, `home`, `cases`, `messages`, `notifications`, `admin`); a template may override with
`{% set nav_active = "match" %}` after `extends`.

Jinja globals: `icon(name, class_="", label=None, size=None)` (inline SVG; names in
`app/utils/icons.py`: paw, home, house-paw, search, search-paw, scan, map-pin, shield, bell,
chat, clipboard, report, rescue, heart, user, community, calendar, clock, camera, image, upload,
check, check-circle, x, plus, arrow-right, arrow-left, chevron-down, menu, logout, settings,
admin, info, warning, question, phone, mail, directions, external, crosshair, send, gift, flag,
lock, eye-off, edit, trash, filter, copy, refresh, list, dog, cat, sparkle), `view_url`,
`directions_url`, `tel_href`, `S` (strings), `public_config`, `csrf_token()`, `config`.
Context: `unread_counts.notifications`, `unread_counts.messages`, `current_year`, `css_parts`.
Filters: `timeago` ("3 days ago"), `date` ("Sept 16, 2026"), `datetime`, `time`, `iso_local`
(for `<input type=datetime-local>`), `km` ("0.6 km"), `masked_phone` ("+63 9** *** 1234"),
`phone` ("+63 917 123 4567"), `peso` ("₱1,500"), `label('ReportStatus')`.

Macros:
- `partials/ui.html`: `page_header(title, accent=None, subtitle=None, icon_name=None, art=True, accent_first=False, level=1)` (supports `{% call %}`), `disclaimer()`, `empty_state(title, text, action_url, action_label, icon_name="paw")`, `stepper(steps, current=1, show_count=True)`, `status_badge(status, label=None)`, `similarity_badge(percent, tier)`, `callout(title=None, kind="success"|"info"|"warn"|"danger", icon_name="info", symbol=None)` (body via `{% call %}`), `upload_tips()`, `map_buttons(lat, lng, size="btn-sm")`.
- `partials/cards.html`: `pet_card(card, message_label="Message", detail_label="View Details")`.
- `partials/forms.html`: `csrf_input()`, `form_errors(list)`, `form_field(wtforms_field, hint=None, label=None, class="", **attrs)`, `input_field(name, label, value='', type='text'|'textarea'|'select'|..., required=False, error=None, hint=None, options=None, id=None, class="", **attrs)`, `checkbox_field(name, label, checked=False, required=False, error=None, hint=None, value='1', id=None)`, `toggle_group(name, label, options=[(value, label, icon?)], value=None, required=False, error=None, hint=None, tiles=False)`, `photo_dropzone(name="photos", label="Photos", max_files=5, required=False, error=None, hint=None, capture=False, id=None)`, `location_picker(prefix="", lat=None, lng=None, place=None, required=True, error=None, label="Location", place_label="Barangay / landmark", hint="Tap the map then pin the location.", id="location")` -> fields `<prefix>lat`, `<prefix>lng`, `<prefix>place`.
- Pages: `errors/coming_soon.html` (stubs), `errors/error.html` (generic; blocks `error_title`, `error_message`, `error_actions`).

---

## 8. JavaScript (ES modules, no build step)

`static/js/main.js` (loaded on every page): `config` (public_config object), `csrfToken()`,
`api(path, {method, json, body, headers, signal})` -> parsed JSON / null; throws
`ApiError {code, message, status, details}`; `toast(message, kind="info"|"success"|"error", {timeout})`,
`escapeHtml(value)`, `compressImage(file, {maxEdge=1600, quality=0.85})`, `openModal(id)`,
`closeModal(idOrEl)`, `initDropzones(root)`. Auto: nav toggle, `[data-dismiss]`,
`[data-open-modal="id"]` / `[data-close-modal]`, `[data-dropzone]` (dispatches `photos-changed`),
location pickers (imports map.js on demand).
`static/js/map.js`: `viewUrl`, `directionsUrl(lat, lng, mode)`, `inServiceArea`,
`initLocationPicker(el)` (returns `{map, setLocation(lat, lng)}`; element fires
`location-change` with `{lat, lng, inArea}`), `initLocationPickers(root)`.
Rule: user content goes in with `textContent` or `escapeHtml()`, never raw `innerHTML`.

---

## 9. CSS components (`static/css/style.css`)

Tokens: `--cream --green --green-strong --green-dark --green-pale --blue --blue-pale --red --red-pale --amber-pale --text --muted --border --radius --shadow --tap`.
Layout: `container(--narrow|--form)`, `section`, `stack(-sm|-lg)`, `cluster(--between|--end)`, `grid-2 grid-3 grid-4`, `split(--results)`, `muted text-green center hide-mobile show-mobile visually-hidden mt-* mb-0`, `h3 h4`.
Buttons: `btn` + `btn-primary` (dark green) `btn-green` `btn-pale` `btn-outline` `btn-blue` `btn-danger` `btn-link`, sizes `btn-sm btn-lg btn-block btn-icon`, group `btn-row`.
Cards: `card(--pale|--blue|--red|--flat|--big)`, `card__header card__body card__footer card__title card-pad`, `tiles tile tile__icon tile__title tile__text`.
Feedback: `callout(--info|--warn|--danger|--success)` + `callout__icon callout__title`, `disclaimer`, `tip`, `empty-state`, `flash(--success|--error|--warning)`, `toast(--success|--error|--info)`, `form-errors`.
Chips: `chip chip--lost|found|closed|sighting|active|matched|expired|hidden|warn|sample`, `similarity-badge(--moderate|--low|--hidden)`, `tier-label(--high)`, `pill-success`.
Forms: `form form-grid field field--full field__label field__hint field__error req checkbox form-actions(--between) form-section form-section__title toggle-group(--tiles) toggle dropzone dropzone__previews dropzone__thumb stepper stepper__step(.is-active|.is-done) stepper__circle stepper__label step-count location-picker location-picker__map location-picker__coords location-picker__status`.
Pet card: `pet-grid pet-card pet-card__media pet-card__badge pet-card__corner pet-card__body pet-card__head pet-card__title pet-card__subtitle pet-card__meta pet-card__why pet-card__actions`.
Scanner: `viewfinder(.is-scanning) viewfinder__media viewfinder__corner--tl|tr|bl|br progress(--indeterminate) progress__bar scan-status scan-summary scan-summary__thumb`.
Chat: `chat chat__header chat__messages chat__composer bubble bubble--mine|theirs|system bubble__time pin-card pin-card__title pin-card__coords checklist conversation-list conversation-list__item(.is-unread)`.
Admin: `table-wrap table num stat-grid stat stat__value stat__label pagination`.
Details: `details-list timeline timeline__item timeline__meta`. Modal: `dialog.modal modal__header modal__title modal__close modal__body modal__footer`.
Page parts: `page-header page-header__title page-header__subtitle page-header__art accent`, `prose`, `coming-soon`, `error-page`.

Extra CSS during the build goes into your `static/css/_parts/<team>.css` (base.html links
every file there); the integrator merges it into style.css. Reuse existing classes first.

---

## 10. Security rules

CSRF is on for every POST/PUT/PATCH/DELETE, including `/api/*`: forms include
`{{ csrf_input() }}` (or `form.hidden_tag()`); `api()` sends `X-CSRFToken` automatically.
CSP and security headers live only in `app/utils/security.py` (allowed: self, Google Fonts,
unpkg.com for Leaflet, tile.openstreetmap.org, res.cloudinary.com, www.gstatic.com for the
Firebase SDK, and the FCM/installations endpoints). Need another origin? Ask the integrator.
Never `|safe` user input. Check authorization with `policy_service` in every route and
test it. Log sighting access (`log_sighting_access`) and admin actions (`log_event("admin.*")`).
Wording: always "possible match" / "possible similarity", never "this is your pet".
