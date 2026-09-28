# PawBalik — Build Specification (Source of Truth)

This file is the single source of truth for building PawBalik. Sections 0–15 are the project brief approved by the proponents. Sections 16–18 add the UI mockup details, the decisions made for this build, and the internal contracts that let several developers work in parallel.

---

## 0. Your Role and How to Work

You are a **senior full-stack software engineer with 15 years of experience** building production web applications in Python/Flask, MySQL, and plain HTML/CSS/JavaScript. You have shipped systems that include third-party AI APIs, cloud media storage, push notifications, and SMS gateways. You write code that is secure by default, easy for a student team to read and maintain, and structured so it can be defended in a Systems Analysis and Design (SAD) panel.

You are building **PawBalik: Lost-Found Dog-Cat Matching Using Gemini API and Rescue Coordination in Tacloban City** for a capstone-style project (IT 122/122L SAD, Leyte Normal University). The proponents are Reynald Jr. A. Cabujoc (leader), Claricel R. Albotra, and Jonalie C. Montera.

Working rules:

1. **Build incrementally, in the milestone order given in Section 12.** Finish and verify one milestone before starting the next. At the end of each milestone, report what was built, how to run it, and what to test manually.
2. **Do not invent APIs.** For any third-party service (Gemini, Cloudinary, Firebase Cloud Messaging, bulksms.com.ph), use the official, current SDK or documented HTTP endpoints. If you are not certain of an endpoint, parameter name, or response shape, say so, isolate it in a single adapter module, and leave a clearly marked `TODO(verify)` comment instead of guessing silently.
3. **Never hardcode secrets.** Every key, password, and token comes from environment variables loaded from `.env`. `.env` must be in `.gitignore`; provide a `.env.example` with placeholder values only.
4. **Prefer boring, well-understood solutions.** No frontend framework, no build step, no ORM magic that a student can't explain. Plain Flask Blueprints, SQLAlchemy, vanilla JS modules, and CSS you can read.
5. **Ask before making a decision that changes scope.** If something is ambiguous, state the assumption explicitly and continue with the most conservative reading.
6. **Human verification is a core principle.** The system never declares that two animals are the same animal or that someone owns a pet. It only surfaces *possible similarities* for humans to review. This must be reflected in the UI copy, the data model, and the API responses.

---

## 1. Product Summary

PawBalik is a web platform that helps citizens of **Tacloban City, Leyte** reunite lost dogs and cats with their owners and coordinate rescue for animals that need help. A user uploads or captures a photo of a dog or cat, and the system uses the **Google Gemini API's multimodal image analysis** to compare it against existing cases, returning a **ranked list of possibly similar animals with a similarity percentage**.

Core flows:

- **Owner lost a pet** → files a *Lost Pet Report* (gets a unique case number) → can run the **Lost Pet Scanner** against private *Pet Sightings* submitted by finders.
- **Citizen found a pet** → runs the **Found Pet Scanner** against public *Lost Pet Reports* → if a possible match appears, opens the case and **messages the owner**, optionally sharing a **location pin** → if no relevant match appears, submits a private **Pet Sighting** with a location pin so the owner can find it later.
- **Animal needs help** → user opens the **Rescue Directory** to find nearby rescuers, veterinary clinics, and shelters with contact details and a route link.
- **Anyone** can open a case or location in **Google Maps** to get directions.
- **Notifications:** when a new possible match is found for someone's case, they receive a **push notification (Firebase Cloud Messaging)** and an **SMS (bulksms.com.ph)**.

---

## 2. Technology Stack (Fixed — Do Not Substitute)

| Layer | Technology | Notes |
|---|---|---|
| Frontend | **HTML5, CSS3, vanilla JavaScript (ES modules)** | Server-rendered Jinja2 templates + progressive enhancement with `fetch()`. Mobile-first, responsive. No React/Vue/Tailwind build step. |
| Backend | **Python 3.11+ with Flask** | Application factory pattern, Blueprints, Flask-Login for sessions, Flask-WTF for CSRF, Flask-Limiter for rate limiting. |
| Database | **MySQL 8.x** | InnoDB, `utf8mb4`. SQLAlchemy with PyMySQL driver. Schema kept in `database/schema.sql` (generated from the models, see Section 7). |
| Image analysis | **Google Gemini API** | Official `google-genai` Python SDK. Model name configurable via `GEMINI_MODEL`. Structured JSON output. |
| Mapping | **Google Maps Intents / Maps URLs** | `https://www.google.com/maps/search/?api=1&query=LAT,LNG` and `https://www.google.com/maps/dir/?api=1&destination=LAT,LNG`. No paid Maps JavaScript API. (See Section 17 for the pin picker.) |
| Photo storage | **Cloudinary** | Official `cloudinary` Python SDK. Signed uploads only. Store `public_id` and `secure_url`. |
| Push notifications | **Firebase Cloud Messaging (FCM)** | Web push via Firebase JS SDK + service worker on the client; `firebase-admin` Python SDK on the server. |
| SMS | **bulksms.com.ph** | Wrapped in an `SmsProvider` adapter. Used to notify users of new possible matches and for OTP. |

Everything runs with `python run.py` locally plus a local MySQL instance. `requirements.txt` has pinned versions.

---

## 3. Scope Boundaries and Limitations (Enforce in Code and UI)

- **Geography:** Tacloban City, Leyte only. Validate that submitted coordinates fall within a configurable bounding box around Tacloban (bounds in config). Reject or warn on coordinates outside it.
- **Species:** domestic **dogs and cats only**. The Gemini pre-check (6.2) rejects photos that do not contain a dog or cat; species is an enum `('dog','cat')`.
- **Similarity ≠ identity.** Every result screen shows a persistent disclaimer: *"Similarity percentages are visual indicators only. They do not confirm that this is the same animal or establish ownership. Please verify with identifying details before arranging a handover."*
- **Ownership verification is human.** A "Verification checklist" in the conversation screen (unique markings, collar, vet records, older photos); never an automated "confirm owner" button.
- **Photo quality matters.** Show upload tips (good lighting, whole body visible, face clearly visible, one animal per photo). Surface Gemini's image-quality assessment when poor.
- **Messaging is case-scoped.** PawBalik does not replace emergency services; display this in the Rescue section.
- **Routing depends on device GPS and Google Maps.** The app only builds the link.
- **Requires internet and a device with a camera or file upload.**

---

## 4. User Roles

| Role | Capabilities |
|---|---|
| **Guest** | View landing page, browse public Lost Pet Reports (contact details masked), view the Rescue Directory. Cannot scan, report, or message. |
| **Registered User** | Everything a guest can do, plus: file Lost Pet Reports, run both scanners, submit Pet Sightings, message within cases, share location pins, manage own cases, receive notifications. One account can be both owner and finder. |
| **Administrator** | Manage the Rescue/Vet/Shelter directory, moderate reports and sightings (hide spam/abuse), deactivate users, view system statistics (cases opened/closed, scans run, reunions). |

Registration requires **name, email, mobile number (PH format), and password**. Normalize mobile numbers to E.164 (`+639XXXXXXXXX`). Verify the mobile number with a one-time code sent via SMS before the user can receive SMS match alerts.

---

## 5. Functional Requirements

### 5.1 Lost Pet Reporting

A logged-in owner creates a Lost Pet Report with:

- **Photos** (required; 1–5 images; first is the primary photo used for matching)
- Pet name, **species** (dog/cat), **breed** (free text with suggestions; "Mixed/Aspin/Puspin" allowed), **color(s)/pattern**, **size** (small/medium/large), **sex** (male/female/unknown), approximate age (optional)
- **Unique identifying features** (free text: scars, collar, eye color, tail, etc.)
- **Last-seen location** (latitude/longitude + barangay/landmark text)
- **Date and time last seen**
- **Contact information** (defaults to account info; option to show/hide phone publicly)
- **Optional reward** (amount in PHP, or free text)

On save:

1. Upload photos (Section 8).
2. Generate a **unique case number** `PB-YYYY-NNNNNN` (e.g., `PB-2026-000123`) inside a DB transaction.
3. Run **Gemini attribute extraction** on the primary photo (6.3) and store it with the user-entered attributes.
4. **Background match** against existing Pet Sightings. If any candidate exceeds the notification threshold, notify the owner (Section 9).
5. Status starts at `lost`. Allowed transitions: `lost → found → closed`, `lost → closed`. Every transition is recorded in `case_status_history` with actor and timestamp.

### 5.2 Pet Scanner (Two Modes)

Both modes accept a photo via **file upload** or **device camera capture** (`<input type="file" accept="image/*" capture="environment">` plus an optional live `getUserMedia` preview).

**Found Pet Scanner** (finder):

- Searches **only public Lost Pet Reports with status `lost`** (not hidden).
- **Never** searches Pet Sightings.
- If results exist: ranked results (5.3).
- If no result meets the relevance threshold: *"No similar lost-pet reports right now. This does not mean the animal isn't missing — the owner may not have reported yet."* plus a **"Submit as Pet Sighting"** button that pre-fills the sighting form with the scanned photo.

**Lost Pet Scanner** (owner):

- User selects one of **their own** active Lost Pet Reports, *or* uploads a new photo of their pet.
- Searches the **private Pet Sighting pool** (status `active`, not hidden).
- The owner sees sighting details needed to act (photo, location pin, date/time seen, finder's chosen contact channel) **only for sightings returned to them as results**. Log every such access.

### 5.3 Similarity Percentage and Ranked Results

Each result card: primary photo, similarity percentage, species/breed/color, distance (km) from scan location or last-seen location, date, and a short Gemini explanation of **matching features and notable differences**. Sorted by final score descending.

Display tiers (configurable): ≥ 80% "High visual similarity"; 60–79% "Moderate visual similarity"; 40–59% "Low visual similarity"; < 40% hidden by default ("Show weaker results" toggle).

Every scan and its results are persisted (`scans`, `scan_results`).

### 5.4 Pet Sightings (Private)

Created by a finder, typically after an unsuccessful Found Pet Scan. Fields: photo(s), species, optional breed/color/size, **location pin (required)**, **date/time seen**, condition (healthy / injured / unknown), whether the finder is holding the animal, notes, finder contact preference (in-app only, or also phone), finder name/email/phone (prefilled from account).

Rules:

- Sightings are **never listed publicly** and never returned by the Found Pet Scanner.
- Visible only to (a) the finder who created them, (b) admins, (c) an owner whose Lost Pet Scanner (or background match) returned that sighting as a result.
- On creation, background match against active Lost Pet Reports; if a high-similarity report is found, **notify that report's owner** (push + SMS) with a link to the private result. Do **not** reveal the owner's report to the finder automatically.
- Sightings auto-expire (`expired`) after a configurable period (default 30 days) and can be marked `matched`.

### 5.5 Case Details and Tracking

Each Lost Pet Report has a detail page: photos, attributes, status timeline, last-seen location ("Open in Google Maps" / "Get Directions"), reward, "Message Owner" button. Owners see an extra panel: possible matches found so far, conversations, status controls ("Mark as Found", "Close Case"). A "My Cases" dashboard lists the user's reports and sightings with status badges.

### 5.6 Case Messaging and Location Sharing

- A **conversation** is tied to one Lost Pet Report (optionally one Sighting). Participants: the case owner and one other user. One conversation per (case, other user) pair.
- Message types: `text`, `location_pin` (lat/lng + optional label), `image`, `system`.
- Location pin messages render as a card with coordinates, label, and **"Open in Google Maps"** / **"Get Directions"** buttons.
- Delivery: messages stored in DB; the client polls `GET /api/conversations/<id>/messages?after=<id>` every few seconds while open; FCM push notifies the recipient in the background.
- Verification checklist panel inside the conversation, and a "Report this user" link.
- Phone numbers are not exposed in chat unless a user explicitly shares them.

### 5.7 Map and Route

- **Picking a location:** an in-page tap-to-pin map (Section 17), a "Use my current location" button (Geolocation API, accuracy shown), manual lat/lng fine-tuning, and a barangay/landmark text field. A "Check on Google Maps" link. Validate against the Tacloban bounding box.
- **Viewing:** `https://www.google.com/maps/search/?api=1&query=LAT,LNG`
- **Routing:** `https://www.google.com/maps/dir/?api=1&destination=LAT,LNG&travelmode=driving` (user can switch to walking).
- **"Nearby" lists** (cases and providers) computed server-side with Haversine, sortable, each item with Open/Directions buttons.
- All Maps URL construction in `static/js/map.js` and `app/utils/maps.py`.

### 5.8 Rescue Assistance and Coordination

- Admin-managed directory of **rescuers, veterinary clinics, and shelters**: name, type, address, barangay, lat/lng, phone(s), Facebook page, hours, 24/7 flag, services.
- Sorted by distance from the user's location or a selected case location, filterable by type and "open now / 24-7".
- Each card: "Call" (`tel:`), "Open in Google Maps", "Get Directions".
- **"Request Rescue Help"** on a case or sighting page produces a copyable summary (case number, species, condition, location with Maps link, reporter contact). Logged in `rescue_requests`.
- Directory seeded with clearly labeled **SAMPLE** data.

### 5.9 Administration

`/admin` (role-protected): providers CRUD, moderate reports/sightings (hide/unhide with reason), manage users (deactivate), counts (active lost cases, sightings, scans per day, reunions), recent SMS/push logs.

---

## 6. Gemini Similarity Engine

Service modules `app/services/matching_service.py` (pipeline, pools, scoring, background matching) and `app/services/gemini_service.py` (all Gemini API calls). Gemini calls are **backend only**.

### 6.1 Pipeline

1. **Validation pre-check (Gemini):** exactly one dog/cat? species? quality?
2. **Attribute extraction (Gemini):** breed guess, colors, pattern, size, markings.
3. **Candidate retrieval (DB):** same species, correct pool, date window, radius.
4. **Coarse ranking (Python):** attribute overlap + distance + recency → top N.
5. **Visual comparison (Gemini):** query photo vs each candidate photo → visual score.
6. **Final score:** weighted blend → rank → persist → return.

### 6.2 Pre-check JSON

```json
{"contains_animal": true, "species": "dog|cat|other|none", "animal_count": 1,
 "image_quality": "good|fair|poor", "quality_issues": ["blurry"], "is_real_photo": true}
```

Reject (friendly message) if species not dog/cat, `animal_count` ≠ 1, or not a real photo. If quality is `poor`, continue with a warning.

### 6.3 Attribute extraction JSON (stored and reused; each stored image analyzed once)

```json
{"species": "dog", "breed_guess": "Aspin (mixed)", "primary_colors": ["brown","white"],
 "coat_pattern": "bicolor", "coat_length": "short", "size_estimate": "medium",
 "distinctive_marks": ["white blaze on chest"], "accessories": ["red collar"]}
```

### 6.4 Candidate retrieval and coarse ranking

- **Pool rules (privacy-critical):** Found Scanner → `lost_reports WHERE status='lost' AND is_hidden=0`. Lost Scanner → `sightings WHERE status='active' AND is_hidden=0`. Enforced in one function `get_candidate_pool(mode, user)` with unit tests. A user's own records are excluded from their own results.
- Filter by same species, date window (default 60 days), radius (default 15 km).
- Coarse score from attribute overlap (colors, pattern, size, coat length), distance decay, recency. Keep top N (default 15).

### 6.5 Visual comparison

Both images (resized to ~1024 px long edge) in one Gemini request. Prompt: compare stable physical traits (species, body shape, coat color/pattern/length, markings, ear/tail shape, eye color, facial structure); ignore background/lighting/pose; do NOT claim the animals are the same individual; return only JSON:

```json
{"visual_similarity": 0-100, "matching_features": ["..."], "differences": ["..."],
 "confidence": "low|medium|high", "comparison_possible": true}
```

Requirements: structured output (`response_mime_type="application/json"` + `response_schema`); bounded concurrency (`ThreadPoolExecutor(max_workers=4)`) with per-call timeout; retry 429/5xx with exponential backoff (max 3 attempts); on total failure fall back to coarse score and flag `visual_check_unavailable: true`; validate/clamp every response; cache in `image_comparisons` keyed by (query sha256, candidate photo id, model); temperature 0–0.2; never send personal data to Gemini.

### 6.6 Final score

```
final = 0.70*visual + 0.15*attribute + 0.10*distance(100 at 0 km → 0 at radius) + 0.05*recency(100 today → 0 at window edge)
```

Weights in config. Different species → excluded. All component scores stored in `scan_results`.

### 6.7 Background matching

DB-backed `jobs` table polled by the worker (`python run.py worker`). New reports and sightings enqueue matching jobs. In tests/dev a config flag `JOBS_EAGER=True` runs jobs inline.

---

## 7. Database Design (MySQL 8 / MariaDB 10.4 compatible, InnoDB, utf8mb4)

SQLAlchemy models in `app/models/` are the source; `database/schema.sql` is the MySQL/MariaDB DDL generated from them (`python run.py export-schema`, with a test that fails if the file is out of date) and is what `python run.py init-db` (or phpMyAdmin import) uses to create the database. No Alembic. Plus `docs/ERD.md`. Index all FKs, `status`, `species`, and `(lat, lng)`. Minimum tables:

- `users` — id, full_name, email (unique), mobile_e164 (unique), mobile_verified_at, password_hash, role (`user`/`admin`), is_active, sms_opt_in, push_opt_in, last_login_at, created_at, updated_at
- `lost_reports` — id, case_number (unique), owner_id, pet_name, species, breed, colors, size, sex, age_text, identifying_features, last_seen_lat, last_seen_lng, last_seen_place, last_seen_at, contact_name, contact_email, contact_phone, show_phone, reward_amount, reward_text, status (`lost`/`found`/`closed`), is_hidden, hidden_reason, hidden_by, hidden_at, ai_attributes (JSON), created_at, updated_at
- `case_counters` — year (PK), last_value (transaction-safe case numbers)
- `sightings` — id, finder_id, species, breed, colors, size, condition, is_holding_animal, notes, seen_lat, seen_lng, seen_place, seen_at, contact_name, contact_email, contact_phone, contact_pref, status (`active`/`matched`/`expired`), is_hidden, hidden_reason, hidden_by, hidden_at, ai_attributes (JSON), expires_at, created_at, updated_at
- `photos` — id, owner_type (`lost_report`/`sighting`/`scan`/`message`), owner_id, storage (`cloudinary`/`local`), public_id, secure_url, width, height, sha256, is_primary, sort_order, ai_attributes (JSON), created_at
- `case_status_history` — id, lost_report_id, from_status, to_status, changed_by, note, changed_at
- `scans` — id, user_id, mode (`found`/`lost`), trigger (`manual`/`background`), query_photo_id, source_lost_report_id, source_sighting_id, scan_lat, scan_lng, precheck (JSON), attributes (JSON), status, error, result_count, gemini_ok, created_at
- `scan_results` — id, scan_id, candidate_type (`lost_report`/`sighting`), candidate_id, candidate_photo_id, visual_score, attribute_score, distance_km, distance_score, recency_score, final_score, tier, rank, visual_check_unavailable, explanation (JSON), notified_at, created_at
- `image_comparisons` — id, query_sha256, candidate_photo_id, model_name, result (JSON), created_at (unique on the triple)
- `conversations` — id, lost_report_id, sighting_id, owner_id, other_user_id, created_at, last_message_at (unique on lost_report_id + other_user_id)
- `messages` — id, conversation_id, sender_id, type, body, lat, lng, location_label, photo_id, read_at, created_at
- `notifications` — id, user_id, type, title, body, link_url, is_read, created_at
- `device_tokens` — id, user_id, fcm_token (unique), user_agent, last_seen_at, created_at
- `sms_log` — id, user_id, to_number, message, purpose, provider, provider_message_id, status, error, created_at
- `push_log` — id, user_id, token_id, title, status, error, created_at
- `phone_verifications` — id, user_id, mobile_e164, code_hash, expires_at, attempts, consumed_at, created_at
- `assistance_providers` — id, name, type (`rescuer`/`vet`/`shelter`), address, barangay, lat, lng, phones, facebook_url, hours_text, is_24_7, services, is_active, is_sample, created_at, updated_at
- `rescue_requests` — id, user_id, lost_report_id, sighting_id, provider_id, summary_text, created_at
- `sighting_access_log` — id, sighting_id, viewer_id, scan_id, accessed_at
- `abuse_reports` — id, reporter_id, target_user_id, conversation_id, reason, status, created_at
- `audit_logs` — id, actor_id, event, target_type, target_id, ip, details (JSON), created_at
- `jobs` — id, type, payload (JSON), status, attempts, last_error, run_after, locked_at, created_at, updated_at

`python run.py seed` (code in `database/seed.py`) creates an admin, SAMPLE providers, and sample lost reports and sightings with local placeholder images.

---

## 8. Photo Storage (Cloudinary)

- `CLOUDINARY_URL` (or cloud name/key/secret) in `.env`. Uploads go through the backend; never expose the secret.
- Validate on server: JPEG/PNG/WebP (HEIC if supported), max 8 MB, verify by file header (Pillow), not extension.
- Folders: `pawbalik/{env}/lost_reports/{id}/`, `.../sightings/{id}/`, `.../scans/{yyyy-mm}/`, `.../messages/{conversation_id}/`.
- Store public_id, secure_url, dimensions, SHA-256 of original bytes.
- Thumbnails `c_fill,w_400,h_400`; Gemini input `c_limit,w_1024,q_auto,f_jpg`. **Strip EXIF** before upload.
- Unused scan photos deleted after 30 days by a cleanup job.
- Local fallback storage (no Cloudinary configured) writes to `uploads/pets/<lost_reports|sightings|scans|messages>/...` at the project root (git-ignored) with unguessable file names, served through an app route.

---

## 9. Notifications

| Event | Recipient | In-app | Push | SMS |
|---|---|---|---|---|
| New possible match (≥ notify threshold, default 75%) | Owner | ✅ | ✅ | ✅ |
| New message in a case conversation | Other participant | ✅ | ✅ | ❌ |
| Case status changed | Conversation participants | ✅ | ✅ | ❌ |
| Mobile verification OTP | User | ❌ | ❌ | ✅ |

All through `NotificationService.notify(user, event, data)` → in-app record, then push/SMS dispatch via the job queue, honoring preferences.

**FCM:** Firebase JS SDK from the official CDN; `firebase-messaging-sw.js` served at site root; permission requested only after a user action ("Enable match alerts"); `POST /api/devices` with the token. Server uses `firebase-admin` with service-account JSON at `FIREBASE_CREDENTIALS_PATH` (git-ignored). Remove invalid tokens. HTTPS required (localhost exempt).

**bulksms.com.ph:** `app/services/sms_service.py` — a `BulkSmsPhProvider` class behind `SmsProvider.send(to_e164, message) -> SmsResult`. Unverified endpoint details marked `TODO(verify)`. Env: `BULKSMS_API_KEY`, `BULKSMS_SENDER_NAME`, etc. Keep messages ≤160 GSM-7 chars when possible. Log in `sms_log`. Per-user limit (max 5 match SMS/day); respect `sms_opt_in` and verified mobile. `ConsoleSmsProvider` for dev via `SMS_PROVIDER=console|bulksms`.

---

## 10. Security, Privacy, Data Protection (RA 10173 spirit)

- Argon2 (or bcrypt) passwords. Cookies `HttpOnly`, `Secure` in production, `SameSite=Lax`.
- CSRF on all state-changing forms and JSON endpoints (`X-CSRFToken` header for `fetch`).
- **Authorization on every endpoint** — sighting visibility, conversation participation, case ownership. Tests for each.
- Flask-Limiter: scans (20/hour/user), report creation, login, OTP, messages.
- SQLAlchemy parameterized queries only; Jinja autoescape; never `|safe` on user input; in JS never inject user content via `innerHTML` without escaping.
- Mask phones publicly (`+63 9** *** 1234`) unless owner opts in.
- Privacy Notice page; consent checkbox at registration and before submitting a sighting with location.
- Audit log for login failures, admin actions, sighting access.
- Security headers: CSP (only needed origins), `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`.

---

## 11. Project Structure (MANDATORY — defined by the proponents)

The proponents' required layout is below. Items marked `(+)` are support files the layout needs that the proponents' sketch did not show; keep them minimal. Do NOT create other top-level folders or other packages (no `blueprints/`, no `migrations/`, no `scripts/`, no `worker.py`, no `cli/`).

```
PawBalik/
│
├── app/
│   ├── __init__.py                 # create_app(): config, extensions, register all route blueprints, CLI
│   ├── config.py (+)               # all settings from .env
│   ├── extensions.py (+)           # db, login_manager, csrf, limiter
│   │
│   ├── routes/                     # one Flask Blueprint per file (page routes + its /api JSON routes)
│   │   ├── auth.py                 # register, login, logout, profile/settings
│   │   ├── main.py (+)             # landing, about, privacy, error pages, local media serving
│   │   ├── scanner.py              # Found/Lost Pet Scanner pages, scanning, results, POST /api/scans
│   │   ├── pets.py                 # Lost Pet Reports: 5-step create wizard, public browse, edit, /api/lost-reports
│   │   ├── cases.py                # case details page, My Cases dashboard, status changes + history
│   │   ├── sightings.py            # private Pet Sightings: create, detail, /api/sightings
│   │   ├── messages.py             # inbox, conversation thread, location pins, /api/conversations
│   │   ├── rescue.py               # rescue/vet/shelter directory, Request Rescue Help, /api/providers
│   │   ├── notifications.py (+)    # notification list, phone OTP verification, /api/devices, service worker
│   │   └── admin.py (+)            # admin dashboard, providers CRUD, moderation, users, logs
│   │
│   ├── models/                     # SQLAlchemy models, one file per group of tables
│   │
│   ├── services/
│   │   ├── gemini_service.py       # every Gemini API call (precheck, attributes, compare)
│   │   ├── matching_service.py     # candidate pools, scoring, run_scan, background matching
│   │   ├── storage_service.py (+)  # Cloudinary / local uploads
│   │   ├── notification_service.py (+) # in-app notifications + Firebase push dispatch
│   │   ├── sms_service.py (+)      # bulksms.com.ph + console SMS providers, OTP sending
│   │   ├── message_service.py (+)  # conversation helpers used by several routes
│   │   ├── job_service.py (+)      # DB-backed background job queue + worker loop
│   │   ├── policy_service.py (+)   # who may see/edit what (sighting privacy etc.)
│   │   └── audit_service.py (+)    # audit log helper
│   │
│   ├── utils/ (+)                  # small helpers: geo, maps, phone, case_numbers, validators, http, auth, time, filters
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html              # landing page
│   │   ├── login.html
│   │   ├── register.html
│   │   ├── dashboard.html          # My Cases dashboard
│   │   ├── profile.html (+) about.html (+) privacy.html (+)
│   │   ├── partials/ (+)           # shared Jinja macros (pet card, disclaimer, location picker, stepper...)
│   │   ├── errors/ (+)             # 403/404/429/500
│   │   ├── scanner/
│   │   │   ├── choose.html         # Upload Photo / Capture Photo tiles (found + lost modes)
│   │   │   ├── scan.html           # scanning state (viewfinder + progress)
│   │   │   └── results.html        # ranked results
│   │   ├── lost_pet/
│   │   │   ├── create_report.html  # 5-step wizard
│   │   │   ├── details.html        # case details
│   │   │   └── browse.html (+) edit_report.html (+)
│   │   ├── sightings/ (+)  messages/ (+)  rescue/ (+)  notifications/ (+)  admin/ (+)
│   │
│   └── static/
│       ├── css/
│       │   └── style.css           # the ONE stylesheet for the whole site
│       ├── js/
│       │   ├── main.js             # api() fetch wrapper + CSRF, escapeHtml, toasts, image compression, nav
│       │   ├── scanner.js          # scanner upload/capture/progress/results + sighting modal
│       │   ├── map.js              # Google Maps URL builders + Leaflet tap-to-pin location picker + geolocation
│       │   ├── messages.js         # chat thread polling, send text/pin/photo
│       │   └── lost_pet.js (+) notifications.js (+) rescue.js (+) admin.js (+)
│       └── images/                 # logo, hero/dog-cat illustration, icons, samples/
│
├── uploads/
│   └── pets/                       # local photo storage when Cloudinary is not configured (git-ignored)
│
├── database/
│   ├── schema.sql                  # MySQL/MariaDB DDL generated from the models (import in phpMyAdmin or run.py init-db)
│   └── seed.py (+)                 # sample admin, SAMPLE providers, demo reports/sightings
│
├── tests/ (+)                      # pytest: conftest.py + test_*.py per feature
├── docs/ (+)                       # SPEC.md, CONTRACTS.md, SETUP.md, API.md, ERD.md, USER_GUIDE.md
├── .env                            # secrets (never committed)
├── .env.example (+) .gitignore (+) pytest.ini (+)
├── requirements.txt
├── run.py                          # entry point (see below)
└── README.md
```

`run.py` is the single entry point:

- `python run.py` — start the development server
- `python run.py init-db` — create tables (executes `database/schema.sql` on MySQL/MariaDB; `create_all` on SQLite)
- `python run.py export-schema` — regenerate `database/schema.sql` from the models
- `python run.py seed [--reset-demo]` — sample data (`database/seed.py`)
- `python run.py worker [--once]` — background job worker (matching, notifications, expiry, cleanup)
- `python run.py match-test <photo> [--mode found|lost]` — try the matching engine from the terminal
- `python run.py eval-matching <folder>` — evaluate similarity scores on labeled photo pairs

(`flask --app run <command>` equivalents may also exist, but documentation uses `python run.py ...`.)

Key REST endpoints (documented in `docs/API.md`):

```
POST   /api/scans                      GET /api/scans/<id>
POST   /api/lost-reports               GET /api/lost-reports?near=lat,lng&species=
GET    /api/lost-reports/<id>          PATCH /api/lost-reports/<id>/status
POST   /api/sightings                  GET /api/sightings/mine
POST   /api/conversations              GET /api/conversations
GET    /api/conversations/<id>/messages?after=<id>
POST   /api/conversations/<id>/messages
GET    /api/providers?near=lat,lng&type=
POST   /api/rescue-requests
POST   /api/devices                    DELETE /api/devices/<token>
GET    /api/notifications              POST /api/notifications/<id>/read
```

JSON errors: `{"error": {"code": "string", "message": "human readable"}}`.

---

## 12. Milestones

1. **Foundation** — factory, config, DB + `database/schema.sql`, `run.py`, base layout, auth, `.env.example`, README.
2. **Lost Pet Reporting** — uploads, report wizard, case numbers, detail page, My Cases, status history, public browse.
3. **Gemini Engine** — precheck, attributes, pools, coarse ranking, visual comparison, scoring, caching; `python run.py match-test <photo>`.
4. **Pet Scanner UI** — both modes, camera capture, loading states, ranked results, "Submit as Pet Sighting".
5. **Pet Sightings** — private creation, visibility rules, access logging, expiry job, background matching.
6. **Messaging & Location Pins** — conversations, polling, pins, Maps buttons, verification checklist.
7. **Notifications** — in-app, FCM, bulksms.com.ph + console, OTP verification, job queue + `python run.py worker`.
8. **Rescue Directory & Admin** — providers CRUD, nearby search, rescue summaries, moderation, stats.
9. **Hardening** — rate limits, security headers, full tests, accessibility, seed data, docs.

---

## 13. UI/UX Guidelines

- **Mobile-first**, tap targets ≥ 44 px, sticky bottom nav on mobile (Home, Scan, Report, Messages, Rescue), prominent Scan.
- Warm identity, WCAG AA contrast, visible focus, `alt` text, labels on every field.
- Clear scan progress states ("Checking photo… Comparing with 12 reports…").
- Plain language; optional Filipino/Waray microcopy ("Nawawala" / "Nakita"); strings in one place.
- Guiding empty states.
- Low-end Android friendly: client-side compression (~1600 px), lazy-loaded thumbnails, small JS.

---

## 14. Testing and Quality

- pytest unit tests: candidate pool privacy rules, scoring math, case numbers, phone normalization, Haversine, Maps URL builders, authorization checks, status transitions.
- Integration tests on SQLite with **mocked** Gemini, Cloudinary, FCM, SMS. No test calls a paid external API.
- `python run.py eval-matching <folder>` for labeled photo pairs (same pet / different pet) → average scores per group.
- `ruff` + `black`; type hints on services.

---

## 15. Acceptance Criteria

1. A new user can register, verify their mobile number, and file a Lost Pet Report that receives a `PB-YYYY-NNNNNN` number and appears in the public list.
2. A different user can scan that pet with the Found Pet Scanner and see it ranked with similarity %, explanation, and disclaimer.
3. A finder whose scan returns nothing relevant can submit a Pet Sighting with a location pin; it is not visible to other finders or the public, but the owner's Lost Pet Scanner (and background matching) can surface it, and the owner gets push + SMS alerts for high-similarity matches.
4. Owner and finder can message within a case, share a location pin, and open it in Google Maps with directions.
5. Users can find nearby rescuers, vets, and shelters sorted by distance, call them, get directions, and generate a rescue request summary.
6. Owners can mark cases found/closed with a visible status history.
7. Admins can manage providers, moderate content, and view statistics.
8. No secrets committed; `.env.example`, `README.md`, `docs/SETUP.md`, `docs/API.md`, `docs/ERD.md` exist and are accurate; all tests pass.

---

## 16. UI Mockups (Design Reference — match these)

### 16.1 Visual language

- **Background:** warm off-white/cream (~`#F7F7F0`) with soft pale-green organic blob shapes in page corners.
- **Primary green:** ~`#4E8B3A` (buttons, accents); **dark green** ~`#2F6B2A` (primary action buttons such as "Message", "Submit", "Report as pet sightings"); **pale green** ~`#EAF2E3` (tiles, callouts, header of modals).
- **Accent cards:** pale blue ~`#DCE8F7` with blue ~`#3A6FB0`; pale red ~`#FDE3E3` with red ~`#E25555`.
- **Text:** near-black headings, grey (~`#6B7280`) secondary text. Font: Inter (Google Fonts) with system fallback. Logo wordmark "PawBalik" in a bold green script/italic display font (e.g., Pacifico) with fallback.
- White cards with 12–16 px radius, subtle border (`#DDE8D5`) and soft shadow. Pill-shaped buttons.

### 16.2 Header (all pages)

Logo left: green line icon of a **house containing a paw**, the "PawBalik" wordmark, and small tagline **"Tacloban City's Lost & Found Pet Platform"**. Centered nav: **Home · Report a Lost Pet · Match a Found Pet · Rescue · About Us** (active item bold green). Right: **"Log In"** and **"Sign Up"** green pill buttons for guests; for logged-in users: Messages, notifications bell with unread count, My Cases, account menu (Profile/Settings, Admin if admin, Log out). On mobile the nav collapses and a sticky bottom nav appears (Home, Scan, Report, Messages, Rescue).

### 16.3 Landing page

- Left: headline **"Bringing Lost Paws Back Home."** (extra-bold, "Home." in green with a brush-stroke underline). Subtitle: *"Photo matching and community rescue coordination for lost and found dogs and cats in Tacloban City."*
- Three action cards in a row: **Report Lost Pet** (pale green, magnifier+paw icon, "Report your lost pet to find possible sightings." →), **Match a Found Pet** (pale blue, paw icon, "Upload or scan a found pet to find its owner." →), **Find Rescue Help** (pale red, map-pin icon, "Find shelters, vets & rescuers near you." →). *(The mockup labels the third card "Report Found Pet" but describes rescue; the description wins, so it links to `/rescue`.)*
- Small shield icon note: "For dogs and cats only".
- Right: hero image of a dog and a cat over a large pale-green circular blob (use `static/images/hero.png` if present, otherwise a bundled SVG illustration).
- Below: a white rounded feature strip with 4 columns separated by thin dividers, each with a green line icon: **Scan Matching** ("Helps match lost and found pets."), **Nearby Help** ("Locate shelters, vets and rescuers near you instantly."), **Updates** ("Get updates about potential matches and case updates."), **Community Driven** ("Stronger together. Help pets, help people, build a kinder community.").

### 16.4 Match a Found Pet — Upload/Capture

Page title with green paw icon: "Match a **Found Pet**" ("Found Pet" green); small dog+cat image at top right over a green blob. A large white rounded card containing two pale-green tiles side by side: **Upload Photo** (image icon, "Choose a photo from your device.", green "Upload" button) and **Capture Photo** (camera icon, "Take a photo using your camera.", green "Capture" button). Below, a tip line with paw icon: "**Tip:** Make sure that pet is clearly visible for better results." The owner-side **Lost Pet Scanner** reuses this layout ("Scan for **My Lost Pet**") with a selector of the user's own active reports.

### 16.5 Scanning state

Same card; the chosen photo shown inside **green viewfinder corner brackets**, a green progress bar beneath, and status text "Scanning for matches…" that advances through stages (Checking photo → Reading features → Comparing with N reports → Ranking).

### 16.6 Results

- Heading: "We found **3 possible matches**" (count in green).
- Left: small card with the scanned photo thumbnail, "Your scanned photo", and a green "✓ Scan Complete" pill.
- Right: pale-green callout with a "?" icon: **"None of these look right?"** — "That's okay — not every found pet has a matching report yet. Submit the pet you scanned as a new sighting so owners searching their pet can find it." — dark-green button **"Report as pet sightings"**.
- "Possible Matches" heading, then a 3-column grid (1 column on mobile) of cards: photo on top with a **"● 84% match"** badge at bottom-right; pet name bold with a **"Lost 3 days ago"** chip on the right; grey line "Pomeranian · White · Male"; pin icon "Last seen 0.6 km away, Brgy. San Jose"; calendar icon "Reported Sept 16, 2026"; person icon "Owner: Jonalie Montera"; buttons **"View Details"** (pale) and **"Message"** (dark green, chat icon). Also show the tier label, a collapsible "Why similar?" (matching features / differences), and the persistent disclaimer (Section 3).

### 16.7 Report a Pet Sighting (modal on the results page, also a standalone page)

Pale-green header "Report a Pet Sighting" with round close (×) button. Sections with green icons: **"Where did you find this pet?"** (map area; hint "Tap the map then pin the location."; plus "Use my current location" and place text); **"When did you find this pet?"** (date + time); **"Your Information"** (Full name; Email Address; Contact Number — prefilled from account). Additional required spec fields (species, condition, holding the animal, notes, contact preference, consent checkbox) fit in a compact "About the pet" section. Footer: **Cancel** (pale) and **Submit** (dark green).

### 16.8 Report a Lost Pet — 5-step wizard

"Step 1 of 5" label and a horizontal 5-circle stepper (active circle filled green, connected by lines): **1 Pet Information · 2 Identifying Features · 3 Where & When Lost · 4 Contact & Reward · 5 Review and Submit**. Section header "Pet Information" / "Please provide the basic details about your lost pet." with the dog+cat image at right. Step 1 grid: **Type of Pet** (two toggle buttons with dog/cat icons), **Pet's Name\***, **Breed\***, **Fur Color / Pattern\***, **Size\***, **Sex\*** (two toggles: Male/Female, plus Unknown), **Estimated Age (Optional)**, **Photo(s) of Your Pet\*** (drop zone, up to 5, first = primary), hint box "Clear photos help more people identify your pet." Back/Next buttons; all steps validated client-side and server-side; works without JS as a single long form.

---

## 17. Build Decisions and Assumptions

1. **Pin picker map.** The mockups require "Tap the map then pin the location", which Google Maps URLs cannot do. The pin picker uses **Leaflet 1.9.x + OpenStreetMap tiles** loaded from a CDN (free, no key), wrapped in `static/js/map.js`, with Geolocation and manual lat/lng fallback if the map fails to load. **All viewing and routing still use Google Maps URLs.** Config `MAP_PICKER=leaflet|none`.
2. **Database.** Production target MySQL 8; the local machine has XAMPP **MariaDB 10.4** — code must work on both (no MySQL-8-only syntax; collation `utf8mb4_unicode_ci`). Tests use SQLite in-memory.
3. **Local fallbacks so the app runs without every key:** `STORAGE_BACKEND=auto|cloudinary|local` (auto = Cloudinary if configured, else local files under `uploads/pets/` served by an app route with unguessable names); push disabled with a log line if Firebase isn't configured; `SMS_PROVIDER=console` by default; if `GEMINI_API_KEY` is missing or Gemini fails, scans fall back to coarse ranking with `visual_check_unavailable`.
4. **Pet name** is required in the wizard (per mockup).
5. **Owner name** appears on public result cards (per mockup); phone numbers stay masked unless the owner opts in.
6. **Background match records** are stored as `scans` with `trigger='background'`, `user_id` = the owner being notified, so sighting visibility is always derived from a scan result the owner owns.

---

## 18. Internal Contracts (parallel development)

These names/signatures are fixed so modules can be built in parallel.

- `app.services.storage_service.save_image(file, *, owner_type, owner_id=None, folder, is_primary=False, sort_order=0) -> Photo` — validates, strips EXIF, hashes, stores (Cloudinary or `uploads/pets/`), adds a `Photo` to the session and flushes (does not commit). Raises `ImageValidationError`.
- `app.services.storage_service.photo_url(photo, variant="full"|"thumb")`, `image_bytes_for_ai(photo) -> bytes` (JPEG ≤1024 px), `delete_photo(photo)`.
- `app.services.gemini_service.GeminiService` — `precheck(image_bytes)`, `extract_attributes(image_bytes)`, `compare(image_a, image_b)`; `is_configured()`.
- `app.services.matching_service.run_scan(*, user, mode, photo, lat=None, lng=None, source_lost_report=None, source_sighting=None, trigger="manual") -> Scan` — raises `ScanRejected(code, message, details)`.
- `app.services.matching_service.match_new_lost_report(report_id)`, `match_new_sighting(sighting_id)` — background; notify via `notify(owner, "possible_match", {...})` for results ≥ `MATCH_NOTIFY_THRESHOLD`.
- `app.services.matching_service.results_for_display(scan, include_weak=False) -> list[dict]`, `get_candidate_pool(mode, user, ...)`.
- `app.services.job_service.enqueue(job_type, payload, run_after=None)`, `@register(job_type)`, `run_pending(limit=10)`, `run_worker(once=False)`. `JOBS_EAGER=True` → run inline. Job types: `match_lost_report`, `match_sighting`, `extract_attributes`, `dispatch_notification`, `expire_sightings`, `cleanup_scan_photos`.
- `app.services.notification_service.notify(user, event, data) -> Notification` — events `possible_match`, `new_message`, `case_status_changed`; push dispatch lives here too.
- `app.services.sms_service.get_provider()`, `send_otp(user) -> None`, `verify_otp(user, code) -> bool`, `send_sms(user, message, purpose) -> SmsResult`.
- `app.services.message_service` — `get_or_create_conversation(lost_report, other_user, sighting=None)`, `post_system_message(conversation, body)`, `conversations_for_report(report)`, `send_message(...)`.
- `app.services.policy_service` — `can_view_sighting(user, sighting)`, `can_view_conversation(user, conv)`, `can_manage_report(user, report)`, `is_admin(user)`.
- `app.services.audit_service.log_event(event, *, actor=None, target=None, details=None)`.
- `app.utils.maps.view_url(lat, lng)`, `directions_url(lat, lng, mode="driving")`; `static/js/map.js` mirrors them.
- `app.utils.geo.haversine_km(a_lat, a_lng, b_lat, b_lng)`, `in_service_area(lat, lng)`.
- `app.utils.phone.normalize_ph(raw) -> str` (raises `ValueError`), `mask_phone(e164)`.
- `app.utils.case_numbers.next_case_number(year=None) -> str`.
- `app.utils.http.json_error(code, message, status=400)`; `app.utils.auth.admin_required`, `api_login_required`.
- Shared front-end: `static/js/main.js` (`api()` fetch wrapper with CSRF + JSON errors, `escapeHtml`, toasts, `compressImage`), `static/js/map.js` (Maps URL builders + location picker); one stylesheet `static/css/style.css`; shared Jinja macros in `templates/partials/` (pet card, disclaimer, location picker, status badge, empty state, stepper).
