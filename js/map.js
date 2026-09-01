/**
 * Leaflet map: building layers, radius selection, bulk ULPIN generation.
 *
 * Works with or without the backend. When the API is unreachable, buildings
 * are fetched from Overpass directly in the browser and floors/units are
 * computed client-side using the same rules as the server.
 */
const MapApp = (() => {
  const CONFIG = {
    center: [9.9816, 76.2999],       // Kochi, Kerala
    zoom: 16,
    maxAreaKm2: 60,
    maxBuildings: 6000,
    overpass: [
      'https://overpass-api.de/api/interpreter',
      'https://overpass.kumi.systems/api/interpreter',
      'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
    ],
  };

  let map, parcelLayer, radiusCircle, selectedLayer = null;
  let buildings = [];               // current feature list
  let selected = null;
  let busy = false;

  /* ------------------------- Height / floor rules -------------------------
     Mirrors backend/app/services/model_3d_generator.py so offline results
     match the server. Keep the two in sync.                                */
  const PROFILES = {
    residential: { typical: 3.5, ground: 4.5, unit: 85, levels: 3 },
    apartments: { typical: 3.5, ground: 4.5, unit: 85, levels: 8 },
    house: { typical: 3.2, ground: 3.5, unit: 110, levels: 2 },
    detached: { typical: 3.2, ground: 3.5, unit: 120, levels: 2 },
    bungalow: { typical: 3.2, ground: 3.5, unit: 130, levels: 1 },
    commercial: { typical: 4.0, ground: 4.5, unit: 150, levels: 5 },
    retail: { typical: 4.0, ground: 4.5, unit: 150, levels: 3 },
    office: { typical: 4.0, ground: 4.5, unit: 150, levels: 8 },
    hotel: { typical: 3.3, ground: 5.0, unit: 45, levels: 9 },
    industrial: { typical: 6.0, ground: 6.0, unit: 400, levels: 1 },
    warehouse: { typical: 8.0, ground: 8.0, unit: 600, levels: 1 },
    school: { typical: 3.6, ground: 4.0, unit: 70, levels: 3 },
    hospital: { typical: 3.8, ground: 4.5, unit: 60, levels: 7 },
    yes: { typical: 3.5, ground: 4.5, unit: 85, levels: 3 },
  };
  const MECH_INTERVAL = 25, MECH_H = 6.0, CORE_EFF = 0.95, MAX_FLOORS = 250;

  const profileFor = (t) => PROFILES[(t || 'yes').toLowerCase()] || PROFILES.yes;

  function heightForLevels(levels, prof) {
    let h = prof.ground;
    for (let n = 2; n <= levels; n++) h += (n % MECH_INTERVAL === 0) ? MECH_H : prof.typical;
    return +h.toFixed(2);
  }

  /** Returns {height, levels|null, source} — levels always win over height. */
  function estimateHeight(tags, buildingType) {
    const prof = profileFor(buildingType);
    const num = (v) => {
      const f = parseFloat(String(v ?? '').replace(',', '.'));
      return Number.isFinite(f) ? f : null;
    };
    let levels = num(tags['building:levels']);
    if (levels && levels > 0 && levels <= MAX_FLOORS) {
      levels = Math.round(levels) + Math.round(num(tags['roof:levels']) || 0);
      const h = num(tags.height) || heightForLevels(levels, prof);
      return { height: h, levels, source: 'OSM building:levels' };
    }
    const h = num(tags.height) || num(tags['building:height']);
    if (h && h > 0) return { height: h, levels: null, source: 'OSM height tag' };
    const dl = prof.levels;
    return { height: heightForLevels(dl, prof), levels: dl, source: `estimated from building=${buildingType}` };
  }

  function calcFloors(heightM, areaM2, buildingType, explicitLevels) {
    const prof = profileFor(buildingType);
    let n, scale = 1;
    if (explicitLevels > 0) {
      n = Math.min(explicitLevels, MAX_FLOORS);
      const implied = heightForLevels(n, prof);
      scale = Math.max(0.6, Math.min(heightM && implied ? heightM / implied : 1, 1.6));
    } else {
      const occupied = heightM * (heightM > 200 ? 0.72 : 1.0);
      n = 0; let acc = 0;
      while (acc < occupied && n < MAX_FLOORS) {
        const nx = n === 0 ? prof.ground : ((n + 1) % MECH_INTERVAL === 0 ? MECH_H : prof.typical);
        if (acc + nx > occupied && n > 0) break;
        acc += nx; n++;
      }
      n = Math.max(1, n);
    }
    const floors = [];
    let elev = 0;
    for (let i = 1; i <= n; i++) {
      const type = i === 1 ? 'ground' : (i % MECH_INTERVAL === 0 ? 'mechanical' : 'typical');
      const fh = +(((type === 'ground' ? prof.ground : type === 'mechanical' ? MECH_H : prof.typical) * scale)).toFixed(2);
      const usable = areaM2 * (i === 1 ? 1 : CORE_EFF);
      floors.push({
        floor_number: i, floor_height_m: fh, base_elevation_m: +elev.toFixed(2),
        floor_area_sq_m: +usable.toFixed(2), floor_type: type,
        units_on_floor: type === 'mechanical' ? 0 : Math.max(1, Math.floor(usable / prof.unit)),
      });
      elev += fh;
    }
    return { floors, total_floors: floors.length, total_units: floors.reduce((s, f) => s + f.units_on_floor, 0) };
  }

  /* ----------------------------- Geometry ----------------------------- */
  function ringMetrics(ring) {
    const pts = ring.slice(0, -1);
    const cy = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    const cx = pts.reduce((s, p) => s + p[0], 0) / pts.length;
    const mLat = 110574, mLon = 111320 * Math.cos(cy * Math.PI / 180);
    let a2 = 0, per = 0;
    for (let i = 0; i < pts.length; i++) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
      const ax = x1 * mLon, ay = y1 * mLat, bx = x2 * mLon, by = y2 * mLat;
      a2 += ax * by - bx * ay;
      per += Math.hypot(bx - ax, by - ay);
    }
    return { lat: cy, lon: cx, area: Math.abs(a2 / 2), perimeter: per, vertices: pts.length };
  }

  const floorColor = (f) =>
    f >= 40 ? '#f43f5e' : f >= 25 ? '#fb923c' : f >= 12 ? '#facc15' : f >= 5 ? '#38bdf8' : '#22d3ee';

  const styleFor = (p) => ({
    color: floorColor(p.properties.total_floors),
    fillColor: floorColor(p.properties.total_floors),
    fillOpacity: 0.38, weight: 1, opacity: 0.9,
  });

  /* ------------------------------- Map -------------------------------- */
  function init() {
    map = L.map('map', { zoomControl: true, preferCanvas: true })
      .setView(CONFIG.center, CONFIG.zoom);

    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19, attribution: 'Imagery &copy; Esri',
    }).addTo(map);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19, opacity: 0.85, attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map);

    parcelLayer = L.layerGroup().addTo(map);
    map.on('moveend', updateRadiusPreview);
    updateRadiusPreview();
    return map;
  }

  /** Dashed circle showing the selected generation radius. */
  function updateRadiusPreview() {
    const km = parseFloat(document.getElementById('radiusSelect')?.value || '1');
    const c = map.getCenter();
    if (radiusCircle) map.removeLayer(radiusCircle);
    radiusCircle = L.circle(c, {
      radius: km * 1000, color: '#818cf8', weight: 1.5,
      dashArray: '7 6', fill: false, interactive: false,
    }).addTo(map);
  }

  /* --------------------------- Overpass (fallback) --------------------- */
  async function overpassBuildings(lat, lon, radiusKm) {
    const q = `[out:json][timeout:90];(way["building"](around:${Math.round(radiusKm * 1000)},${lat},${lon});` +
      `relation["building"](around:${Math.round(radiusKm * 1000)},${lat},${lon}););out geom;`;
    let lastErr = 'unknown';
    for (const url of CONFIG.overpass) {
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'data=' + encodeURIComponent(q),
        });
        if (!res.ok) { lastErr = `HTTP ${res.status}`; continue; }
        const data = await res.json();
        const out = [];
        for (const el of data.elements || []) {
          const tags = el.tags || {};
          if (!tags.building || tags.building === 'roof') continue;
          let g = el.geometry;
          if (!g && el.members) g = (el.members.find(m => m.role === 'outer' && m.geometry) || {}).geometry;
          if (!g || g.length < 3) continue;
          const ring = g.map(n => [n.lon, n.lat]);
          if (ring[0][0] !== ring.at(-1)[0] || ring[0][1] !== ring.at(-1)[1]) ring.push(ring[0]);
          if (ring.length < 4) continue;
          out.push({ el, tags, ring });
        }
        if (out.length) return out;
        lastErr = 'no buildings returned';
      } catch (e) { lastErr = e.message; }
    }
    throw new Error(`Overpass unavailable (${lastErr})`);
  }

  /** Build a GeoJSON feature with computed floors/units, entirely client-side. */
  function featureFromOSM({ el, tags, ring }, index) {
    const m = ringMetrics(ring);
    const btype = tags.building === 'yes' ? 'residential' : tags.building;
    const { height, levels, source } = estimateHeight(tags, btype);
    const bd = calcFloors(height, m.area, btype, levels);

    const name = tags.name || tags['addr:housename'] ||
      (tags['addr:housenumber'] && tags['addr:street'] ? `${tags['addr:housenumber']} ${tags['addr:street']}` : null) ||
      `${String(tags.building).replace(/_/g, ' ')} (OSM ${el.type}/${el.id})`;

    // Deterministic local ULPIN so offline mode still shows a plausible code.
    const st = String(Math.floor(Math.abs(m.lat)) % 40).padStart(2, '0');
    const di = String(Math.floor(Math.abs(m.lon)) % 90).padStart(2, '0');
    const sd = String(Math.floor(Math.abs(m.lat * 1000)) % 999).padStart(3, '0');
    const vi = String(Math.floor(Math.abs(m.lon * 1000)) % 999).padStart(3, '0');
    const pl = String((el.id % 9999) + 1).padStart(4, '0');

    return {
      type: 'Feature',
      geometry: { type: 'Polygon', coordinates: [ring] },
      properties: {
        ulpin: `${st}${di}${sd}${vi}${pl}`,
        name: name.charAt(0).toUpperCase() + name.slice(1),
        building_type: btype,
        area_sq_m: +m.area.toFixed(2),
        height_m: +height.toFixed(2),
        total_floors: bd.total_floors,
        total_units: bd.total_units,
        centroid_lat: m.lat, centroid_lon: m.lon,
        height_source: source,
        osm_id: el.id,
        _floors: bd.floors,       // cached so the 3D modal works offline
        _local: true,
      },
    };
  }

  /* --------------------------- Bulk generation ------------------------- */
  async function generateForRadius() {
    if (busy) return;
    busy = true;
    setGenerating(true);

    const km = parseFloat(document.getElementById('radiusSelect').value);
    const c = map.getCenter();
    const t0 = performance.now();

    try {
      UI.showLoader('Fetching buildings…', `OpenStreetMap · ${km} km radius`, 15);
      let features = [];
      let viaBackend = false;

      if (API.isOnline) {
        try {
          UI.showLoader('Generating ULPINs…', 'Backend is processing the radius', 45);
          const r = await API.bulkGenerate({ lat: c.lat, lon: c.lng, radiusKm: km, persist: true });
          features = r.data.buildings.features || [];
          viaBackend = true;
        } catch (e) {
          UI.toast(`Backend failed (${e.message}). Falling back to browser processing.`, 'warn', 5000);
        }
      }

      if (!viaBackend) {
        UI.showLoader('Fetching buildings…', 'Querying Overpass directly', 35);
        const raw = await overpassBuildings(c.lat, c.lng, km);
        UI.showLoader('Computing floors & units…', `${raw.length} footprints`, 75);
        features = raw.map(featureFromOSM);
      }

      if (features.length > CONFIG.maxBuildings) {
        UI.toast(`Showing the first ${CONFIG.maxBuildings} of ${features.length} buildings.`, 'warn', 5000);
        features = features.slice(0, CONFIG.maxBuildings);
      }

      UI.showLoader('Rendering…', `${features.length} parcels`, 92);
      buildings = features;
      renderBuildings(features);

      const secs = ((performance.now() - t0) / 1000).toFixed(1);
      const units = features.reduce((s, f) => s + (f.properties.total_units || 0), 0);
      UI.hideLoader();
      UI.toast(
        `<b>${features.length.toLocaleString()}</b> buildings · ` +
        `<b>${units.toLocaleString()}</b> units in ${secs}s ` +
        `<span class="text-slate-500">(${viaBackend ? 'backend' : 'browser'})</span>`,
        'success', 6000
      );
    } catch (err) {
      UI.hideLoader();
      UI.toast(err.message || 'Generation failed.', 'error', 7000);
    } finally {
      busy = false;
      setGenerating(false);
    }
  }

  function setGenerating(on) {
    const btn = document.getElementById('generateBtn');
    if (!btn) return;
    btn.disabled = on;
    btn.classList.toggle('opacity-60', on);
    btn.classList.toggle('cursor-not-allowed', on);
    btn.innerHTML = on
      ? '<span class="spinner"></span> Generating…'
      : '<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h8l-1 8 10-12h-8z"/></svg> Generate ULPINs';
  }

  /* ------------------------------ Rendering ---------------------------- */
  function renderBuildings(features) {
    parcelLayer.clearLayers();
    selectedLayer = null;

    const layers = features.map((f) => {
      const coords = f.geometry.coordinates[0].map(([lon, lat]) => [lat, lon]);
      const layer = L.polygon(coords, styleFor(f));
      layer._feature = f;
      layer.on('mouseover', () => layer.setStyle({ fillOpacity: 0.7, weight: 2.5 }));
      layer.on('mouseout', () => { if (layer !== selectedLayer) layer.setStyle(styleFor(f)); });
      layer.on('click', () => select(f, layer));
      // Tooltips bound lazily; binding thousands upfront is slow.
      layer.once('mouseover', () => {
        layer.bindTooltip(
          `${f.properties.name} · ${f.properties.total_floors} fl`,
          { className: 'parcel-tip', sticky: true, direction: 'top' }
        ).openTooltip();
      });
      return layer;
    });

    parcelLayer.addLayer(L.featureGroup(layers));
    updateStats(features);
  }

  function updateStats(features) {
    const bar = document.getElementById('statsBar');
    if (!bar) return;
    if (!features.length) { bar.classList.add('hidden'); return; }
    const floors = features.map(f => f.properties.total_floors);
    const units = features.reduce((s, f) => s + f.properties.total_units, 0);
    const tagged = features.filter(f => (f.properties.height_source || '').startsWith('OSM')).length;
    const tallest = features.reduce((a, b) => b.properties.total_floors > a.properties.total_floors ? b : a);
    bar.innerHTML =
      `<b class="text-sky-300">${features.length.toLocaleString()}</b> buildings` +
      `<span class="dot"></span><b class="text-indigo-300">${units.toLocaleString()}</b> units` +
      `<span class="dot"></span><b class="text-emerald-300">${Math.round(tagged / features.length * 100)}%</b> OSM-tagged` +
      `<span class="dot"></span><b class="text-amber-300">${Math.max(...floors)}</b> max floors` +
      `<span class="dot"></span><span class="text-slate-500 truncate">${tallest.properties.name}</span>`;
    bar.classList.remove('hidden');
  }

  /* ------------------------------ Selection ---------------------------- */
  function select(feature, layer) {
    if (selectedLayer && selectedLayer._feature) selectedLayer.setStyle(styleFor(selectedLayer._feature));
    selectedLayer = layer;
    layer.setStyle({ color: '#ffffff', fillColor: '#10b981', fillOpacity: 0.8, weight: 3 });
    selected = feature;
    Details.show(feature);
  }

  /* -------------------------------- Search ----------------------------- */
  async function searchLocation(query) {
    if (!query.trim()) return;
    UI.showLoader('Searching…', query);
    try {
      const res = await fetch(
        `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`
      );
      const results = await res.json();
      UI.hideLoader();
      if (!results.length) { UI.toast(`No match for “${query}”.`, 'error'); return; }
      map.flyTo([parseFloat(results[0].lat), parseFloat(results[0].lon)], 16, { duration: 1.3 });
      UI.toast(`Moved to ${results[0].display_name.split(',').slice(0, 2).join(',')}. Press Generate.`, 'info', 5000);
    } catch {
      UI.hideLoader();
      UI.toast('Geocoding service unreachable.', 'error');
    }
  }

  function exportGeoJSON() {
    if (!buildings.length) { UI.toast('Generate some buildings first.', 'warn'); return; }
    const clean = buildings.map(f => ({
      ...f,
      properties: Object.fromEntries(Object.entries(f.properties).filter(([k]) => !k.startsWith('_'))),
    }));
    UI.download('ulpin-parcels.geojson', JSON.stringify({ type: 'FeatureCollection', features: clean }, null, 2));
  }

  function exportCSV() {
    if (!buildings.length) { UI.toast('Generate some buildings first.', 'warn'); return; }
    const rows = buildings.map(f => ({
      ulpin: f.properties.ulpin, name: f.properties.name,
      building_type: f.properties.building_type, area_sq_m: f.properties.area_sq_m,
      height_m: f.properties.height_m, total_floors: f.properties.total_floors,
      total_units: f.properties.total_units, lat: f.properties.centroid_lat,
      lon: f.properties.centroid_lon, height_source: f.properties.height_source,
    }));
    UI.download('ulpin-parcels.csv', UI.toCSV(rows), 'text/csv');
  }

  return {
    init, generateForRadius, updateRadiusPreview, searchLocation,
    exportGeoJSON, exportCSV, calcFloors, estimateHeight, ringMetrics,
    get map() { return map; },
    get selected() { return selected; },
    get buildings() { return buildings; },
  };
})();

window.MapApp = MapApp;
