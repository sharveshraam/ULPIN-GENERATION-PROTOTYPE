# Backend ↔ Frontend integration guide

Context for an AI assistant (or developer) working on linking the two halves of
this project. Everything below was verified against the running app, not
inferred from the code.

---

## 1. Shape of the project

Two independently deployed halves, talking over plain HTTP + JSON:

```
repo root
├── index.html          landing page
├── map.html            the app ("console")
├── styles.css          shared styles
├── js/
│   ├── config.js       <-- THE backend URL lives here, and nowhere else
│   ├── api.js          HTTP client, one method per endpoint
│   ├── map.js          Leaflet map, ULPIN generation, ULPIN lookup
│   ├── details.js      right-hand details panel (floors, units)
│   ├── 3d-viewer.js    Three.js building viewer
│   ├── globe.js        decorative rotating Earth on the landing page
│   ├── flyto.js        cinematic "descend from orbit" map transition
│   └── ui.js           toasts, loader, modals
├── app/__init__.py     shim so `app.main:app` resolves from the repo root
├── backend/app/        the real FastAPI package
├── requirements.txt    root-level, for the Render build
└── render.yaml         Render Blueprint
```

- **Frontend**: static files, no build step, no framework. Vanilla JS +
  Leaflet + Three.js loaded from CDNs. Served by GitHub Pages.
- **Backend**: FastAPI + SQLAlchemy + SQLite. Deployed on Render.

There is no bundler, no `npm install`, no transpilation. Scripts are plain
`<script src>` tags and load order matters (see §5).

---

## 2. How the frontend finds the backend

**One place only: `js/config.js`.**

```js
const API_BASE_URL = 'https://your-service.onrender.com';   // no trailing slash
window.API_BASE_URL = API_BASE_URL;
```

`js/api.js` reads it once at load:

```js
function resolveBase() {
  const configured = (typeof window !== 'undefined' && window.API_BASE_URL) || '';
  if (configured) return configured.replace(/\/$/, '');
  if (['localhost', '127.0.0.1'].includes(location.hostname)) return 'http://127.0.0.1:8000';
  return '';                     // same origin
}
```

Deliberate design constraints — **do not reintroduce these**:

- No `?api=` query-string override.
- No `localStorage` override, and no in-app "connect" dialog.
- `API.base` is a **getter only**. There is no setter, no `clearBase()`, no
  `testBase()`. The endpoint cannot be changed at runtime.

Rationale: the owner wants the endpoint fixed in committed code so the deployed
site is deterministic. If you need to point at a different backend, edit
`config.js`.

---

## 3. Contract between the two halves

### Response envelope

Most endpoints return:

```json
{ "success": true, "data": ... }
```

`data` is usually a GeoJSON `Feature` or `FeatureCollection`.

**Two deliberate exceptions:**

- `GET /health` returns a bare object:
  `{"status":"ok","version":"1.0.0","database":"connected","parcels":1}`
- `POST /api/v1/generate-custom-ulpin` returns exactly
  `{"ulpin":"IND-TN-001-CHE-F03-U301"}` — no envelope. This shape was specified
  by the client; do not "normalise" it.

### Endpoints (17, verified from `/openapi.json`)

| Method | Path |
| --- | --- |
| GET | `/` , `/health` |
| POST | `/api/v1/generate-ulpin` , `/api/v1/generate-ulpin/from-coordinates` |
| GET | `/api/v1/decode-ulpin/{ulpin}` |
| POST | `/api/v1/generate-custom-ulpin` |
| GET | `/api/v1/decode-custom-ulpin/{ulpin}` |
| GET/POST | `/api/v1/parcels` |
| GET/DELETE | `/api/v1/parcels/{ulpin}` |
| GET | `/api/v1/parcels/{ulpin}/floors` |
| GET | `/api/v1/parcels/{ulpin}/units` (paginated: `limit`, `offset`, `floor`) |
| POST | `/api/v1/generate-3d-model` |
| POST | `/api/v1/bulk-generate` , `/api/v1/bulk-generate/bbox` |
| GET | `/api/v1/search` (`q` \| `address` \| `lat`+`lon`) |

Every one of these has a matching method in `js/api.js`. **When you add a
backend endpoint, add its client method there too** — nothing else in the
frontend calls `fetch` against the API directly.

### Feature properties

A parcel Feature's `properties` carry:

```
ulpin, name, building_type, area_sq_m, height_m,
total_floors, total_units, centroid_lat, centroid_lon,
height_source, osm_id
```

Client-side (offline) features additionally carry `_floors` and `_local: true`.
Keys prefixed with `_` are internal and are stripped on export.

---

## 4. ULPIN formats

| Digits | Meaning | Example |
| --- | --- | --- |
| 14 | parcel | `32070410180902` |
| 17 | parcel + floor | `32070410180902003` |
| 20 | parcel + floor + unit | `32070410180902003012` |

Layout: `SS DD SSS VVV PPPP` = state(2) district(2) sub-district(3) village(3)
plot(4), then floor(3), then unit(3).

There is also a hyphenated presentation format:
`IND-TN-001-CHE-F03-U301` (`{Country}-{State}-{District}-{City}-{Plot}-{Unit}`).

**Important:** the 14-digit numeric ULPIN is the canonical stored identifier.
The hyphenated form is display-only and encodes **no coordinates**, so it cannot
be resolved to a location unless that parcel already exists in the registry.

Uniqueness (verified): `unique=True` on `parcels.ulpin`, `floors.floor_ulpin`,
`units.unit_ulpin`; plot numbers auto-increment per village; `create_parcel` is
idempotent on `osm_id` so re-scanning an area does not mint duplicates.

Known limitation: admin codes are hash-derived and truncated (district → only
2 digits), so distinct districts can share a code at national scale. This does
**not** create duplicate ULPINs — the plot counter keeps them distinct — but it
makes decoding a ULPIN back to a place lossy. A real deployment would read LGD
codes from a table instead.

---

## 5. Script load order (fragile — respect it)

`map.html`:

```html
<script src="js/config.js"></script>   <!-- must precede api.js -->
<script src="js/api.js"></script>
<script src="js/ui.js"></script>       <!-- UI.toast/openModal used by others -->
<script src="js/3d-viewer.js"></script>
<script src="js/details.js"></script>
<script src="js/map.js"></script>      <!-- last: depends on all of the above -->
```

`index.html` additionally loads three.js then `js/globe.js` (which needs the
`THREE` global) for the decorative background Earth.

`map.html` loads `js/flyto.js` before `js/map.js`. `FlyTo.to(map, lat, lon, opts)`
performs the orbital search transition and is called from both `searchLocation()`
and `focusFeature()`. It falls back to an instant `setView` under
`prefers-reduced-motion` or if a flight is already running, so the map always
ends up at the destination.

`config.js` must load before `api.js`, because `api.js` resolves the base URL at
module-evaluation time. Each file exposes a single global (`API`, `UI`,
`MapApp`, `Details`, `Viewer3D`, `Globe`).

`js/globe.js` is purely decorative and dependency-free with respect to the
backend. `Globe.init()` returns `false` and does nothing if WebGL is
unavailable or the three.js CDN is blocked, so it can never break the page.

---

## 6. Offline / degraded mode

The frontend is designed to work with the backend **absent**. `API.checkHealth()`
sets `API.isOnline`, and when false the app fetches buildings straight from
Overpass and computes floors/units in the browser.

That client-side logic in `js/map.js` (`PROFILES`, `estimateHeight`, `calcFloors`)
is a **deliberate duplicate** of
`backend/app/services/model_3d_generator.py`. **If you change the floor
algorithm on one side, change it on the other**, or online and offline results
will silently diverge.

Floor-count rules, both sides:
1. If OSM `building:levels` exists it is authoritative; storey heights are
   scaled to fit the occupied height (scale clamped 0.6–1.6).
2. Otherwise estimate from height, applying a 0.72 occupancy factor above 200 m
   (quoted heights include spires — Burj Khalifa is 828 m but 163 floors).
3. Every 25th floor is mechanical (6 m, 0 units).

---

## 7. CORS

`backend/app/config.py` reads `ALLOWED_ORIGINS` (falling back to the older
`CORS_ORIGINS`), comma-separated, defaulting to `*`.

Two behaviours to be aware of:

- When origins include `*`, `allow_credentials` is **forced to False** —
  browsers reject `Access-Control-Allow-Origin: *` on credentialed requests.
- When explicit origins are set, common localhost dev ports
  (3000/5500/8080, both `localhost` and `127.0.0.1`) are appended automatically.

For production set `ALLOWED_ORIGINS=https://<user>.github.io` (origin only — no
path, no trailing slash).

The origins in effect are logged at startup:
`CORS origins=[...] credentials=False`.

---

## 8. Running it locally

```bash
# backend
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# frontend, from the repo root, in a second terminal
python3 -m http.server 3000
```

Open `http://localhost:3000/map.html`. With `API_BASE_URL` empty, localhost is
auto-detected and the frontend talks to `http://127.0.0.1:8000`.

Tests: `cd backend && .venv/bin/python -m pytest tests/ -q` → **61 passed**.

---

## 9. Deployment

**Render (backend)** — native Python runtime, no Docker:

| Setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT` |
| Health Check | `/health` |

`requirements.txt` **must** be at the repo root — Render's Python builder does
not look in `backend/`. The root `app/__init__.py` shim re-points the package
path at `backend/app`, so the Start Command also works *without* `--app-dir`.

**GitHub Pages (frontend)** — serves from the `prototype` branch, repo root.

Gotchas that have already bitten this project:

- **SQLite on Render is ephemeral.** No persistent disk on the free tier; all
  generated parcels are lost on redeploy or restart. Attach a Disk or move to
  Postgres before treating data as durable.
- **Free-tier cold starts take ~50 s.** The first request after idling looks
  exactly like an outage.
- **Mixed content**: a Pages site is https and cannot call an http API.
- **Three.js is pinned to r0.128.0** for UMD globals. r148+ removed
  `examples/js/` and r150+ removed `build/three.min.js`; upgrading requires
  switching to an importmap + ES modules.

---

## 10. Current state

- Branches `prototype` and `arena/01a05c3c-ulpin-generation-prototype` are in
  sync. `main` is stale (still the original upload) and is **not** what Pages
  serves.
- `js/config.js` currently has `API_BASE_URL = ''` — **this is why a deployed
  frontend reports "Browser mode"**. Setting it to the Render URL is the one
  remaining step to link the two halves.
