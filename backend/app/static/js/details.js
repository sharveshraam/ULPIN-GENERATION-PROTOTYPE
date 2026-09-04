/**
 * Building details panel + 3D modal (floor list, paginated units, exports).
 */
const Details = (() => {
  let feature = null;
  let floors = [];
  let unitState = { offset: 0, limit: 100, total: 0, floor: null, rows: [] };

  const el = (id) => document.getElementById(id);

  /* ------------------------------- Panel ------------------------------- */
  function show(f) {
    feature = f;
    const p = f.properties;

    el('dTitle').textContent = p.name || 'Unnamed building';
    el('dSubtitle').textContent =
      `${(p.centroid_lat ?? 0).toFixed(5)}°, ${(p.centroid_lon ?? 0).toFixed(5)}° · ${p.building_type}`;

    // ULPIN with colour-coded administrative segments.
    const u = String(p.ulpin || '').padEnd(14, '0');
    el('dUlpin').innerHTML =
      `<span class="text-sky-300">${u.slice(0, 2)}</span>` +
      `<span class="text-indigo-300">${u.slice(2, 4)}</span>` +
      `<span class="text-teal-300">${u.slice(4, 7)}</span>` +
      `<span class="text-amber-300">${u.slice(7, 10)}</span>` +
      `<span class="text-rose-300">${u.slice(10, 14)}</span>`;

    UI.countUp(el('dFloors'), p.total_floors || 0);
    UI.countUp(el('dUnits'), p.total_units || 0);
    el('dHeight').textContent = `${(p.height_m || 0).toFixed(1)} m`;
    el('dArea').textContent = UI.fmtArea(p.area_sq_m || 0);

    const src = p.height_source || 'unknown';
    const verified = src.startsWith('OSM');
    el('dBadges').innerHTML =
      badge(verified ? 'OSM verified' : 'Estimated',
        verified ? 'bg-emerald-500/15 text-emerald-300 border-emerald-400/30'
          : 'bg-amber-500/15 text-amber-300 border-amber-400/30') +
      badge(src, 'bg-white/5 text-slate-400 border-white/10') +
      badge(p._local ? 'Local compute' : 'Backend', 'bg-sky-500/15 text-sky-300 border-sky-400/30');

    el('detailsPanel').classList.remove('hidden', 'translate-y-full');
  }

  const badge = (t, cls) =>
    `<span class="text-[10px] px-2 py-1 rounded-md border ${cls}">${t}</span>`;

  function hide() {
    el('detailsPanel').classList.add('translate-y-full');
    setTimeout(() => el('detailsPanel').classList.add('hidden'), 250);
  }

  /* ------------------------------ 3D modal ----------------------------- */
  async function open3D() {
    if (!feature) return;
    const p = feature.properties;
    UI.openModal('modal3d');
    el('m3dTitle').textContent = p.name || 'Building';
    el('m3dMeta').textContent =
      `ULPIN ${p.ulpin} · ${p.building_type} · ${(p.height_m || 0).toFixed(1)} m`;
    el('floorList').innerHTML = UI.skeleton(6, 'h-9');
    el('unitList').innerHTML = '';

    // Three.js needs the container to have a size, so init after it is visible.
    setTimeout(() => { Viewer3D.init(el('viewer3d')); Viewer3D.resize(); }, 60);

    let model = null;
    if (API.isOnline) {
      try {
        const r = await API.generate3DModel({
          geometry: feature.geometry,
          height_m: p.height_m,
          levels: p.total_floors,
          building_type: p.building_type || 'residential',
          include_unit_geometry: false,
          // This panel reads model.floors and model.building.total_units only.
          // The unit list below is paged separately through
          // /parcels/{ulpin}/units, so the inline units array is never touched:
          // for a 163-storey tower it is ~21k records and ~2.6 MB of JSON that
          // the server has to build and this tab has to parse. Measured on the
          // same request: 156 ms / 2.71 MB with it, 8.9 ms / 89 KB without.
          include_units: false,
        });
        model = r.data;
      } catch (e) {
        UI.toast(`Backend 3D failed (${e.message}). Using local model.`, 'warn');
      }
    }

    if (!model) {
      // Offline: reuse the floors computed when the building was loaded.
      const local = p._floors ||
        MapApp.calcFloors(p.height_m, p.area_sq_m, p.building_type, p.total_floors).floors;
      model = {
        building: {
          ulpin: p.ulpin, total_height_m: p.height_m,
          footprint_area_sq_m: p.area_sq_m,
          estimated_floors: local.length,
          total_units: local.reduce((s, f) => s + f.units_on_floor, 0),
        },
        floors: local.map(f => ({ ...f, floor_ulpin: `${p.ulpin}${String(f.floor_number).padStart(3, '0')}` })),
        units: [],
      };
    }

    floors = model.floors || [];
    setTimeout(() => {
      const info = Viewer3D.render(model, feature.geometry.coordinates[0]);
      el('m3dStats').innerHTML =
        `<b class="text-sky-300">${info.floors}</b> floors` +
        `<span class="dot"></span><b class="text-indigo-300">${(model.building.total_units || 0).toLocaleString()}</b> units` +
        `<span class="dot"></span><b class="text-teal-300">${info.height.toFixed(1)} m</b> modelled`;
    }, 120);

    renderFloorList();
    unitState = { offset: 0, limit: 100, total: 0, floor: null, rows: [] };
    loadUnits();

    Viewer3D.onFloorPick = (f) => {
      unitState.floor = f.floor_number;
      unitState.offset = 0;
      document.querySelectorAll('[data-floor-row]').forEach(r =>
        r.classList.toggle('floor-active', +r.dataset.floorRow === f.floor_number));
      loadUnits();
    };
  }

  function renderFloorList() {
    if (!floors.length) { el('floorList').innerHTML = '<p class="text-xs text-slate-500">No floor data.</p>'; return; }
    // Render newest (top) floor first, matching how a tower is read.
    el('floorList').innerHTML = [...floors].reverse().map(f => `
      <button data-floor-row="${f.floor_number}"
        onclick="Details.focusFloor(${f.floor_number})"
        class="floor-row w-full text-left px-3 py-2 rounded-lg flex items-center gap-3 text-[11px]">
        <span class="w-9 shrink-0 font-mono text-slate-400">F${f.floor_number}</span>
        <span class="w-14 shrink-0 text-slate-500">${f.floor_height_m} m</span>
        <span class="flex-1 ${f.floor_type === 'mechanical' ? 'text-slate-500 italic' : 'text-slate-300'}">
          ${f.floor_type === 'mechanical' ? 'plant / mechanical' : `${f.units_on_floor} units`}
        </span>
        <span class="text-slate-600">${f.base_elevation_m} m</span>
      </button>`).join('');
  }

  function focusFloor(n) {
    Viewer3D.highlightFloor(n);
    unitState.floor = n;
    unitState.offset = 0;
    document.querySelectorAll('[data-floor-row]').forEach(r =>
      r.classList.toggle('floor-active', +r.dataset.floorRow === n));
    loadUnits();
  }

  function clearFloorFilter() {
    Viewer3D.resetHighlight();
    unitState.floor = null;
    unitState.offset = 0;
    document.querySelectorAll('[data-floor-row]').forEach(r => r.classList.remove('floor-active'));
    loadUnits();
  }

  /** Units are paginated: a tall tower can have tens of thousands. */
  async function loadUnits() {
    const box = el('unitList');
    box.innerHTML = UI.skeleton(4, 'h-7');
    const p = feature.properties;

    let rows = [], total = 0;
    const stored = !p._local && API.isOnline;

    if (stored) {
      try {
        const r = await API.getUnits(p.ulpin, {
          floor: unitState.floor, limit: unitState.limit, offset: unitState.offset,
        });
        rows = r.data; total = r.total;
      } catch { /* fall through to local generation */ }
    }

    if (!rows.length) {
      // Generate the page locally from the floor table.
      const src = unitState.floor ? floors.filter(f => f.floor_number === unitState.floor) : floors;
      const all = [];
      for (const f of src) {
        for (let u = 1; u <= f.units_on_floor; u++) {
          all.push({
            unit_ulpin: `${p.ulpin}${String(f.floor_number).padStart(3, '0')}${String(u).padStart(3, '0')}`,
            floor_number: f.floor_number, unit_number: u,
            area_sq_m: +(f.floor_area_sq_m / Math.max(1, f.units_on_floor)).toFixed(2),
          });
        }
      }
      total = all.length;
      rows = all.slice(unitState.offset, unitState.offset + unitState.limit);
    }

    unitState.total = total;
    unitState.rows = rows;

    if (!total) { box.innerHTML = '<p class="text-xs text-slate-500 py-3">No units on this floor.</p>'; }
    else {
      box.innerHTML = rows.map(u => `
        <div class="unit-chip" title="${u.unit_ulpin}">
          <span class="font-mono text-[10px] text-sky-300">${u.unit_ulpin.slice(-6)}</span>
          <span class="text-[10px] text-slate-500">F${u.floor_number}</span>
          <span class="text-[10px] text-slate-400">${u.area_sq_m} m²</span>
        </div>`).join('');
    }

    const from = total ? unitState.offset + 1 : 0;
    const to = Math.min(unitState.offset + unitState.limit, total);
    el('unitMeta').innerHTML =
      `Showing <b class="text-slate-300">${from}–${to}</b> of <b class="text-slate-300">${total.toLocaleString()}</b>` +
      (unitState.floor ? ` on floor ${unitState.floor} <button onclick="Details.clearFloorFilter()" class="text-sky-400 hover:underline ml-1">(clear)</button>` : '');
    el('unitPrev').disabled = unitState.offset === 0;
    el('unitNext').disabled = to >= total;
  }

  function pageUnits(dir) {
    unitState.offset = Math.max(0, unitState.offset + dir * unitState.limit);
    loadUnits();
  }

  /* ------------------------------ Exports ------------------------------ */
  function exportFloors() {
    if (!floors.length) return;
    UI.download(`${feature.properties.ulpin}-floors.csv`, UI.toCSV(floors), 'text/csv');
  }

  function exportBuildingGeoJSON() {
    if (!feature) return;
    const clean = {
      ...feature,
      properties: Object.fromEntries(Object.entries(feature.properties).filter(([k]) => !k.startsWith('_'))),
    };
    UI.download(`${feature.properties.ulpin}.geojson`, JSON.stringify(clean, null, 2));
  }

  function copyUlpin() {
    if (feature) UI.copy(feature.properties.ulpin, `ULPIN ${feature.properties.ulpin} copied`);
  }

  return {
    show, hide, open3D, focusFloor, clearFloorFilter,
    pageUnits, exportFloors, exportBuildingGeoJSON, copyUlpin,
    get feature() { return feature; },
  };
})();

window.Details = Details;
