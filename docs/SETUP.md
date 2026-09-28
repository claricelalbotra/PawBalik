# PawBalik Setup Guide (Windows + XAMPP)

This guide takes a fresh Windows PC to a running PawBalik. Every command runs in the project folder (`PawBalik/`) in **Command Prompt**, **PowerShell** or **Git Bash**.

## 1. Install the tools

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 or newer (tested on 3.13) | Tick **"Add python.exe to PATH"** during install |
| XAMPP | any recent (MariaDB 10.4+) | Only **MySQL** is needed; Apache is optional (for phpMyAdmin) |
| A modern browser | Chrome / Edge / Firefox | Camera capture needs HTTPS or `localhost` |

## 2. Start MySQL

Open the **XAMPP Control Panel** → click **Start** next to **MySQL**. The XAMPP default account is `root` with no password, and `.env.example` already uses it.

## 3. Create the virtual environment

```bash
python -m venv .venv
.venv\Scripts\activate              # PowerShell / cmd
# source .venv/Scripts/activate     # Git Bash
python -m pip install -r requirements.txt
```

## 4. Create `.env`

Copy `.env.example` to `.env`, then fill in what you have. Every key is optional except `SECRET_KEY` in production. Without a key, the matching feature falls back as shown:

| Key(s) | Where to get it | Without it |
|---|---|---|
| `SECRET_KEY` | Any long random string (`python -c "import secrets; print(secrets.token_hex(32))"`) | A development key is used |
| `DATABASE_URL` | Default `mysql+pymysql://root:@localhost/pawbalik?charset=utf8mb4` | — |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Choose them | Seed prints a generated admin password once |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/) → **Get API key** | Scans use coarse ranking only (attributes, distance, date), marked "visual check unavailable" |
| `GEMINI_MODEL` | Default `gemini-2.5-flash` | — |
| `CLOUDINARY_URL` | Cloudinary Console → **Dashboard** → *API environment variable* (`cloudinary://KEY:SECRET@CLOUD`) | Photos are saved in `uploads/pets/` |
| `FIREBASE_CREDENTIALS_PATH` | Firebase Console → Project settings → **Service accounts** → *Generate new private key* (save the JSON outside git, e.g. `instance/firebase.json`) | Push notifications are off |
| `FIREBASE_WEB_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_STORAGE_BUCKET`, `FIREBASE_MESSAGING_SENDER_ID`, `FIREBASE_APP_ID` | Firebase Console → Project settings → **General** → *Your apps* → Web app config | "Enable match alerts" is hidden |
| `FIREBASE_VAPID_KEY` | Firebase Console → Project settings → **Cloud Messaging** → *Web Push certificates* → Generate key pair | Push is off |
| `SMS_PROVIDER=bulksms`, `BULKSMS_API_URL`, `BULKSMS_API_KEY`, `BULKSMS_SENDER_NAME` | Your bulksms.com.ph account dashboard / API documentation | `SMS_PROVIDER=console` prints SMS (and OTP codes) in the terminal |

> **bulksms.com.ph:** the adapter (`BulkSmsPhProvider` in `app/services/sms_service.py`) could not be checked against the provider's official API docs. Lines marked `TODO(verify)` must be confirmed with your account's documentation before you send real SMS.

Never commit `.env` or the Firebase JSON file; both are in `.gitignore`.

## 5. Create the database

Pick one:

- **Command:** `python run.py init-db` creates the `pawbalik` database and all 22 tables from `database/schema.sql`.
- **phpMyAdmin:** start Apache too, open http://localhost/phpmyadmin → **Import** → choose `database/schema.sql` → **Go**.

The script uses `IF NOT EXISTS`, so running it again is safe. After changing a model, run `python run.py export-schema` to regenerate `database/schema.sql`.

### Upgrading a database created before profile pictures were added

`init-db` does not change tables that already exist. If your `pawbalik` database was created before profile pictures were added, run this once in phpMyAdmin (**SQL** tab):

```sql
ALTER TABLE photos MODIFY owner_type ENUM('lost_report','sighting','scan','message','avatar') NOT NULL;
ALTER TABLE users ADD COLUMN avatar_photo_id INTEGER NULL AFTER privacy_consented_at,
  ADD INDEX ix_users_avatar_photo_id (avatar_photo_id),
  ADD CONSTRAINT fk_users_avatar_photo_id_photos FOREIGN KEY (avatar_photo_id)
    REFERENCES photos (id) ON DELETE SET NULL;
```

## 6. Add sample data

```bash
python run.py seed
```

This creates:
- The admin account from `ADMIN_EMAIL`/`ADMIN_PASSWORD`.
- 10 **SAMPLE** rescue/vet/shelter providers. They are clearly labelled; replace them with verified real entries in **Admin → Providers**.
- 3 demo users. Their password is printed once.
- 6 demo lost reports and 4 demo sightings.

`python run.py seed --reset-demo` removes and recreates only the demo data.

## 7. Run the app and the worker

Terminal 1:

```bash
python run.py                        # http://127.0.0.1:5000
```

Terminal 2 (background matching, notifications, sighting expiry, photo cleanup):

```bash
python run.py worker
```

For a quick demo without the worker, set `JOBS_EAGER=true` in `.env` so jobs run during the request (slower pages).

## 8. Try the matching engine from the terminal

```bash
python run.py match-test path\to\dog.jpg --mode found --lat 11.2445 --lng 125.0036
python run.py eval-matching path\to\pairs      # folder with same\ and different\ subfolders of image pairs
```

`eval-matching` prints average scores for same-pet and different-pet pairs and suggests a threshold. Use it to justify the tier thresholds in your paper.

## 9. Tests and code quality

```bash
python -m pytest -q          # 720 tests, SQLite in-memory, all external services mocked
python -m ruff check .
python -m black --check .
```

## 10. Push notifications need HTTPS

Browsers allow service workers and push only on `https://` or `http://localhost`. To test push from a phone, deploy behind HTTPS or use a tunnel (for example `ngrok http 5000`), and add that URL to Firebase's authorized domains.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot connect to MySQL - is XAMPP MySQL started?` | Start MySQL in the XAMPP Control Panel; check `DATABASE_URL` |
| Scans say "visual check unavailable" | `GEMINI_API_KEY` is missing or invalid, or the Gemini API is unreachable |
| The pin-drop map is blank | The picker loads Leaflet from unpkg.com and tiles from OpenStreetMap, so it needs internet. You can still type the latitude/longitude or use "Use my current location" |
| "Use my current location" does nothing | The browser blocked location; allow it, and use `localhost` or HTTPS |
| No SMS arrives | With `SMS_PROVIDER=console`, codes are printed in the terminal running `python run.py` |
| Port 5000 in use | `python run.py --port 5001` |
