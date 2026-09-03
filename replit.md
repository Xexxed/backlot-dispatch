# Backlot Dispatch on Replit

## Run

The project uses the existing Python FastAPI stack. Install dependencies with:

```sh
pip install -r requirements.txt
```

Start the application with:

```sh
python -m uvicorn app.main:app --host 0.0.0.0 --port 5000
```

The Replit workflow **Start application** runs this command and exposes port
5000 as the web preview.

## Environment

Runtime configuration is read from Replit Secrets. The app supports the
following configured values:

- `APP_SECRET`
- `GEMINI_MODEL`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_GENAI_USE_VERTEXAI`
- `GOOGLE_MAPS_API_KEY` (optional — enables the manual live weather refresh;
  without it the dashboard serves the committed fixtures in `seed/weather/`)
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `NOW_OVERRIDE` (optional HH:MM — pins the AD console's "now" so recorded
  demos stay timezone-stable; Replit servers run UTC)
- `PUBLIC_BASE_URL`
- `TRUSTED_HOSTS`

`GOOGLE_SERVICE_ACCOUNT_JSON` is materialized to a local credentials file at
startup when Vertex AI is enabled. Never commit `.env` files or credential
files.

## Data and local development

The demo production data is committed under `seed/`. Runtime state is stored
in SQLite at `instance/backlot.db` by default. The app creates the database
schema on startup. To regenerate the deterministic demo CSVs, run:

```sh
python scripts/seed_demo.py
```