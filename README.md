# PawBalik

**Lost-Found Dog-Cat Matching Using Gemini API and Rescue Coordination in Tacloban City**
IT 122/122L Systems Analysis and Design capstone, Leyte Normal University.
Proponents: Reynald Jr. A. Cabujoc (leader), Claricel R. Albotra, Jonalie C. Montera.

PawBalik helps people in Tacloban City reunite lost dogs and cats with their owners.
An owner files a Lost Pet Report. A finder scans a photo of a found pet, and Google
Gemini lists **possibly similar** animals with a similarity percentage. The app never
says two animals are the same pet; people confirm that themselves. When nothing
similar appears, the finder saves a private Pet Sighting that the owner's own scans
can surface later. A Rescue Directory lists nearby rescuers, vets and shelters.

- Specification: [`docs/SPEC.md`](docs/SPEC.md)
- Developer contracts (endpoint names, models, fixtures, helpers): [`docs/CONTRACTS.md`](docs/CONTRACTS.md)

## Tech stack

Python 3.11+ (tested on 3.13) · Flask 3 · SQLAlchemy 2 · MySQL 8 / MariaDB 10.4 (XAMPP) ·
Jinja2 templates with plain CSS and vanilla JavaScript (ES modules, no build step) ·
Google Gemini (`google-genai`) · Cloudinary · Firebase Cloud Messaging · bulksms.com.ph.

## Quick start (Windows + XAMPP)

1. **Start MySQL.** Open the XAMPP Control Panel and click **Start** next to *MySQL*.

2. **Create a virtual environment and install the packages** (in the project folder):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate            # Git Bash: source .venv/Scripts/activate
   python -m pip install -r requirements.txt
   ```

3. **Configure.** Copy `.env.example` to `.env` and edit it. The defaults work with
   XAMPP (`root` user, no password, database `pawbalik`). You can leave the Gemini,
   Cloudinary, Firebase and SMS keys empty: photos are then stored in `uploads/pets/`,
   push is off, SMS go to the terminal, and scans use coarse ranking only.
   Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` to get an admin account from the seed step.

4. **Create the database and tables.** Either run:

   ```bash
   python run.py init-db
   ```

   or open phpMyAdmin (http://localhost/phpmyadmin), choose **Import**, select
   `database/schema.sql` and click **Go**.

5. **Add sample data** (admin account, SAMPLE rescue providers, demo cases):

   ```bash
   python run.py seed                # add --reset-demo to recreate the demo data
   ```

6. **Run the app:**

   ```bash
   python run.py                     # http://127.0.0.1:5000  (--port 5001 to change)
   ```

7. **Run the background worker** in a second terminal (matching, notifications,
   sighting expiry, photo cleanup):

   ```bash
   python run.py worker              # Ctrl+C to stop; --once runs due jobs and exits
   ```

   For a simple demo without a worker, set `JOBS_EAGER=true` in `.env` so jobs run
   during the request.

## All commands

| Command | What it does |
|---|---|
| `python run.py` | Development server (`--host`, `--port`, `--no-debug`) |
| `python run.py init-db` | Create tables (MySQL/MariaDB: runs `database/schema.sql`; SQLite: `create_all`) |
| `python run.py export-schema` | Regenerate `database/schema.sql` after changing a model |
| `python run.py seed [--reset-demo]` | Admin account, SAMPLE providers, demo reports/sightings |
| `python run.py worker [--once]` | Background job worker |
| `python run.py match-test <photo> [--mode found\|lost]` | Try the matching engine from the terminal |
| `python run.py eval-matching <folder>` | Average similarity scores on labelled photo pairs |

The same commands exist as `flask --app run <command>`.

If the app says *"Cannot connect to MySQL - is XAMPP MySQL started?"*, start MySQL in
the XAMPP Control Panel and try again.

## Tests and code style

```bash
python -m pytest -q                  # SQLite in memory; Gemini, Cloudinary, FCM and SMS are mocked
ruff check .
black .
```

No test calls a paid external API.

## Project layout

```
PawBalik/
├── app/
│   ├── __init__.py            create_app()
│   ├── config.py, extensions.py
│   ├── routes/                auth, main, scanner, pets, cases, sightings, messages,
│   │                          rescue, notifications, admin (one blueprint per file)
│   ├── models/                SQLAlchemy models (22 tables)
│   ├── services/              gemini_service, matching_service, storage_service,
│   │                          notification_service, sms_service, message_service,
│   │                          job_service, policy_service, audit_service
│   ├── utils/                 geo, maps, phone, case numbers, validators, ...
│   ├── templates/             base, index, login, register, dashboard, profile, about,
│   │                          privacy + scanner/ lost_pet/ sightings/ messages/ rescue/
│   │                          notifications/ admin/ partials/ errors/
│   └── static/                css/style.css · js/main.js scanner.js map.js messages.js
│                              lost_pet.js notifications.js rescue.js admin.js · images/
├── uploads/pets/              local photo storage when Cloudinary is not configured
├── database/                  schema.sql (generated from the models) · seed.py
├── tests/                     pytest suite (conftest.py has the shared fixtures)
├── docs/                      SPEC, CONTRACTS, SETUP, API, ERD, USER_GUIDE
├── requirements.txt
├── run.py                     the single entry point
└── README.md
```

More documentation: [Setup guide](docs/SETUP.md) · [User guide](docs/USER_GUIDE.md) ·
[JSON API](docs/API.md) · [ERD](docs/ERD.md).

## Privacy and limitations

- Tacloban City only (coordinates are checked against a bounding box), dogs and cats only.
- Similarity percentages are visual indicators only. They do not confirm that an animal is
  the same pet or establish ownership.
- Pet Sightings are private. Phone numbers are masked unless the owner opts in.
- Secrets live only in `.env`, which is git-ignored. See the Privacy Notice page in the app.
