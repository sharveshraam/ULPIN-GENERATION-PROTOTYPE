# ULPIN Atlas — 3D ULPIN Generation & Vertical Property Mapping

Generate standards-compliant Indian **ULPINs** (Bhu-Aadhaar) for real building
footprints, then model vertical property rights floor by floor and unit by unit.

| Layer | Stack |
|---|---|
| Frontend | Vanilla JS, Leaflet 1.9, Three.js r128, Tailwind (CDN) |
| Backend | FastAPI, SQLAlchemy 2, Shapely, SQLite (Postgres-ready) |
| Data | OpenStreetMap Overpass + Nominatim (free, no API keys) |

---

## ULPIN structure

```
Parcel   14 digits   [State 2][District 2][Sub-District 3][Village 3][Plot 4]
Floor    17 digits   parcel + [Floor 3]
Unit     20 digits   parcel + [Floor 3][Unit 3]
```

Example: `32070410180902` → floor 12 → `32070410180902012` → unit 4 → `32070410180902012004`

### Human-readable format

An alternative hyphenated presentation is also supported:

```
{Country}-{State}-{District}-{City}-{Plot}-{Unit}
IND-TN-001-CHE-F03-U301
```

| Part | Rule | Example |
|---|---|---|
| Country | 3 uppercase letters | `IND` |
| State | 2 uppercase letters | `TN` |
| District | 3 digits | `001` |
| City | 3 uppercase letters | `CHE` |
| Plot | letter + 2 digits | `F03` |
| Unit | `U` + 3 digits | `U301` |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/generate-custom-ulpin \
  -H 'Content-Type: application/json' \
  -d '{"country":"IND","state_code":"TN","district_code":"001","city_code":"CHE","plot_code":"F03","unit_code":"U301"}'
# {"ulpin":"IND-TN-001-CHE-F03-U301"}
```

The numeric 14-digit ULPIN remains the canonical identifier; ULPIN columns are
`String(50)` so both forms fit.

---

## Quick start

### 1. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional; defaults work as-is
uvicorn app.main:app --reload --port 8000
```

Interactive API docs: <http://127.0.0.1:8000/docs>

### 2. Frontend

The frontend is fully static — serve the repo root with anything:

```bash
python3 -m http.server 3000      # then open http://127.0.0.1:3000
```

The frontend auto-detects the backend at `http://127.0.0.1:8000` when you are on
localhost. To point it elsewhere (e.g. a Cloud Run URL) append `?api=`:

```
http://127.0.0.1:3000/map.html?api=https://your-service.run.app
```

> **Browser mode.** If the backend is unreachable the app still works — it queries
> Overpass directly and computes floors/units client-side using identical rules.
> The header pill tells you which mode you are in.

### 3. Tests

```bash
cd backend && python -m pytest tests/ -q      # 34 tests
```

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | API info |
| `GET` | `/health` | Health + parcel count |
| `GET` | `/api/v1/parcels` | All buildings as a GeoJSON FeatureCollection |
| `GET` | `/api/v1/parcels/{ulpin}` | Single building |
| `POST` | `/api/v1/parcels` | Create a building (ULPIN auto-allocated) |
| `DELETE` | `/api/v1/parcels/{ulpin}` | Delete (cascades to floors + units) |
| `GET` | `/api/v1/parcels/{ulpin}/floors` | Floor table |
| `GET` | `/api/v1/parcels/{ulpin}/units` | Units, paginated (`limit`, `offset`, `floor`) |
| `POST` | `/api/v1/generate-ulpin` | Build a ULPIN from admin codes |
| `POST` | `/api/v1/generate-custom-ulpin` | Hyphenated `IND-TN-001-CHE-F03-U301` form |
| `GET` | `/api/v1/decode-custom-ulpin/{ulpin}` | Split a hyphenated ULPIN into parts |
| `POST` | `/api/v1/generate-ulpin/from-coordinates` | Derive codes by reverse geocoding |
| `GET` | `/api/v1/decode-ulpin/{ulpin}` | Split a 14/17/20-digit ULPIN into parts |
| `POST` | `/api/v1/generate-3d-model` | Floors + units + per-floor 3D geometry |
| `POST` | `/api/v1/bulk-generate` | Every building within a radius |
| `POST` | `/api/v1/bulk-generate/bbox` | Every building in a viewport |
| `GET` | `/api/v1/search` | By ULPIN/name (`q`), proximity (`lat`+`lon`), or address |

### Example

```bash
curl -X POST http://127.0.0.1:8000/api/v1/bulk-generate \
  -H 'Content-Type: application/json' \
  -d '{"center_lat":9.9816,"center_lon":76.2999,"radius_km":1.0,"persist":true}'
```

---

## How floor counts stay accurate

A naive `floors = height / 3.5` is wrong for tall towers, because published
heights include spires that contain no floors. The Burj Khalifa is 828 m but has
**163 occupied floors** — the naive formula claims 236.

The rule used here:

1. **Tagged storey counts win.** If OSM supplies `building:levels`, that is
   authoritative and floor heights are fitted to the occupied height.
2. **Otherwise estimate from height**, applying a 0.72 occupancy factor above
   200 m so architectural spires do not inflate the count.
3. **Ground floors are taller** (4.5 m), **mechanical floors** appear every 25
   storeys at 6 m and carry no saleable units, and usable floorplate is reduced
   to 95 % for lift cores and risers.

Both the Python backend and the JavaScript fallback implement these identical
rules — verified by a parity test across six building profiles.

---

## Deployment

**Backend → Render (native Python runtime, no Docker)**

Create a new **Web Service** on Render, point it at this repo, and use:

| Setting | Value |
| --- | --- |
| Environment | `Python 3` |
| Root Directory | *(leave blank — use the repo root)* |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/health` |

Two details matter:

- `requirements.txt` must be at the **repo root** — that is where Render's
  Python builder looks. The older `backend/requirements.txt` is the dev/test
  manifest (it adds `pytest`) and is kept for local work.
- The FastAPI package lives at `backend/app`, so `app.main:app` only resolves
  when uvicorn is given `--app-dir backend`. Never hard-code the port; Render
  injects `$PORT` and the service must bind `0.0.0.0`.

Environment variables to set in the dashboard:

| Variable | Purpose |
| --- | --- |
| `ALLOWED_ORIGINS` | Your frontend origin, e.g. `https://<user>.github.io`. Use `*` for an open demo. Comma-separate multiple origins. |
| `DATABASE_URL` | Optional. Defaults to `sqlite:///./ulpin_database.db`. |
| `LOG_LEVEL` | Optional, defaults to `INFO`. |
| `PYTHON_VERSION` | Optional, e.g. `3.11.9`. |

`render.yaml` at the repo root encodes all of the above as a Blueprint if you
prefer infrastructure-as-code over dashboard clicks.

> **SQLite is ephemeral on Render.** The free tier has no persistent disk, so
> every redeploy, restart or instance move wipes generated parcels. That is fine
> for a demo (the DB is rebuilt on demand), but attach a Render Disk or point
> `DATABASE_URL` at Postgres before treating any data as durable.

When `ALLOWED_ORIGINS` is `*`, the API automatically disables
`allow_credentials`, because browsers reject a wildcard origin on credentialed
requests. When explicit origins are set, common localhost dev ports are appended
so local development keeps working against the deployed API.

**Backend → Google Cloud Run (alternative)**

```bash
cd backend
gcloud run deploy ulpin-api --source . --region asia-south1 --allow-unauthenticated
```

The `Dockerfile` installs GEOS (required by Shapely) and honours Cloud Run's
`$PORT`. Set `DATABASE_URL` to a managed Postgres instance for persistence —
SQLite on Cloud Run is wiped on every cold start.

**Frontend → GitHub Pages**

Enable Pages on the repo (Settings → Pages → deploy from the branch root), then
tell the frontend where the backend lives. There are three ways, checked in this
order — the first one found wins:

1. **Query parameter** — good for a quick test, nothing to commit:
   `https://<user>.github.io/<repo>/map.html?api=https://ulpin-api.onrender.com`
   The value is remembered afterwards.

2. **The "Connect API" dialog** — click the status pill in the header, paste the
   URL, press *Test & save*. It calls `/health` first and reports a clear error
   if the service is asleep, the URL is wrong, or CORS is blocking the origin.
   Saved in `localStorage`, so it survives reloads but only on that browser.

3. **`js/config.js`** — the permanent option, and the one to use so that *every*
   visitor gets a working site:

   ```js
   const API_BASE_URL = 'https://ulpin-api.onrender.com';   // no trailing slash
   ```

   Commit it and Pages picks it up on the next deploy.

Whichever you choose, the backend must allow the Pages origin. On Render set:

```
ALLOWED_ORIGINS = https://<user>.github.io
```

(no path, no trailing slash), or `*` for an open demo. If it is left unset while
the frontend is on a different domain, every request fails CORS and the app
silently drops into browser mode.

Two gotchas worth knowing:

- **Mixed content.** A Pages site is https, so the API must be https too. A
  `http://` API URL is blocked by the browser. Render gives you https, so just
  don't hand-edit it back to http.
- **Free-tier cold starts.** Render sleeps idle services; the first request can
  take ~50 seconds and may look like a failure. Retry once before assuming the
  wiring is wrong — the status pill will flip to "API live" when it wakes.

If the backend is unreachable the app still works: it computes floors and units
client-side and shows "Browser mode" in the header.

---

## Testing checklist

- [ ] `pytest` — 61 tests pass
- [ ] `/health` returns `{"status":"ok","database":"connected"}`
- [ ] `/docs` renders the full endpoint list
- [ ] Landing page: nav, scroll-reveal, API status pill
- [ ] Map: pick a radius, press **Generate ULPINs**, buildings appear colour-coded
- [ ] Click a building → details panel with a 14-digit ULPIN
- [ ] **View 3D model** → orbitable tower; hover shows floor info; click filters units
- [ ] Unit registry paginates (Prev/Next) instead of showing only a handful
- [ ] GeoJSON + CSV export download
- [ ] Stop the backend and reload — the app runs in browser mode and still works

---

## Known limitations

- **Administrative codes are approximate.** State codes use the real LGD list,
  but district/sub-district/village codes are deterministic hashes of the
  reverse-geocoded name, not official LGD registry values. Production use
  requires the actual LGD dataset.
- **Plot numbers are per-village sequences** allocated by this database, so they
  do not correspond to real survey numbers.
- **Unit layouts are estimates.** Units are derived from floorplate area divided
  by a typical unit size and subdivided on a grid; they are not real floor plans.
- **Unit rows are capped** at `PERSIST_UNITS_LIMIT` (4000) per building. A
  163-floor tower implies ~13,800 units; the API reports the true total but only
  stores the first 4000. The 3D endpoint returns all of them without persisting.
- **Overpass rate-limits.** Large radii on the public API can return 429; the
  client falls back across three mirrors and reports failure clearly.
- **Rate limiting is in-process**, so it resets on restart and is per-instance.
  Use a shared store (Redis) behind a load balancer.
- **Not implemented:** dark/light toggle, STL export, side-by-side comparison,
  shareable per-building links, authentication.

---

## Project layout

```
├── index.html            Landing page
├── map.html              Map console + 3D modal
├── styles.css            Shared styling
├── js/
│   ├── api.js            Backend client (auto-detects, degrades gracefully)
│   ├── ui.js             Toasts, loaders, modals, scroll reveal, exports
│   ├── map.js            Leaflet, Overpass fallback, floor/unit rules
│   ├── details.js        Details panel, floor list, paginated units
│   └── 3d-viewer.js      Three.js building viewer
└── backend/
    ├── app/
    │   ├── main.py               FastAPI app + endpoints
    │   ├── database.py           SQLAlchemy models
    │   ├── schemas.py            Pydantic validation
    │   ├── crud.py               DB operations
    │   ├── config.py             Environment config
    │   └── services/
    │       ├── ulpin_generator.py
    │       ├── geometry_processor.py
    │       ├── model_3d_generator.py
    │       └── osm_fetcher.py
    ├── tests/test_api.py
    ├── requirements.txt
    ├── Dockerfile
    └── .env.example
```

Data © OpenStreetMap contributors (ODbL). Imagery © Esri.
