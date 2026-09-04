/**
 * Three.js building viewer.
 *
 * Renders a real footprint extruded floor by floor. Each floor is a separate
 * mesh so it can be hovered, clicked and colour-banded. Footprint coordinates
 * are projected from lon/lat to local metres before extrusion, otherwise a
 * building would be ~100000x wider than it is tall.
 *
 * Built for a low-CPU host and low-end clients:
 *   - three.js (~600 KB) is fetched the first time the viewer is opened, not
 *     on every map page load. Most visitors never open the 3D modal.
 *   - Nothing paints unless something changed. There is no idle rAF loop: a
 *     163-floor tower redrawn 60x/second while the user reads a list is the
 *     single most expensive thing this page can do.
 *   - The footprint is triangulated ONCE and stretched per floor instead of
 *     re-running earcut for every storey.
 */
const Viewer3D = (() => {
  // Pinned to match map.html: three r128 is the last line that ships UMD
  // globals (build/three.min.js and examples/js/*). r148+ removed
  // examples/js/, r150+ removed build/three.min.js. Newer releases are
  // ES-module only, which would need an importmap and break this setup.
  const THREE_SRC = 'https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js';
  const ORBIT_SRC = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js';

  let renderer, scene, camera, controls, raycaster, pointer;
  let floorMeshes = [];
  let model = null;
  let hovered = null;
  let frameId = null;               // pending rAF handle, or null when idle
  let container = null;
  let onFloorPick = null;
  let loading = null;               // in-flight three.js load, if any
  let pending = null;               // render() called before the libs arrived
  let started = false;              // init() has kicked off (or finished) setup
  let wantedHighlight = null;       // floor asked for before geometry existed

  const M_PER_DEG_LAT = 110574;

  /* ------------------------------ lazy loading ---------------------------- */

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      // Reuse a tag the page may still ship, so nothing downloads twice.
      const existing = document.querySelector(`script[src="${src}"]`);
      if (existing) {
        if (existing.dataset.loaded) { resolve(); return; }
        existing.addEventListener('load', () => resolve());
        existing.addEventListener('error', () => reject(new Error(src)));
        return;
      }
      const s = document.createElement('script');
      s.src = src;
      s.async = false;              // keep three before OrbitControls
      s.dataset.loaded = '';
      s.onload = () => { s.dataset.loaded = '1'; resolve(); };
      s.onerror = () => reject(new Error(src));
      document.head.appendChild(s);
    });
  }

  /**
   * Resolve once THREE and THREE.OrbitControls exist. Downloads them the first
   * time; every later call is a resolved promise. Rejects if the CDN is
   * unreachable, so the caller can say so instead of hanging.
   */
  function ensureThree() {
    if (typeof THREE !== 'undefined' && THREE.OrbitControls) return Promise.resolve();
    if (loading) return loading;
    loading = (typeof THREE === 'undefined' ? loadScript(THREE_SRC) : Promise.resolve())
      .then(() => {
        if (typeof THREE !== 'undefined' && THREE.OrbitControls) return;
        return loadScript(ORBIT_SRC);
      })
      .catch((err) => { loading = null; throw err; });
    return loading;
  }

  /* --------------------------------- colour -------------------------------- */

  const BAND_STOPS = [
    [0.00, [0x22, 0xd3, 0xee]],
    [0.35, [0x38, 0xbd, 0xf8]],
    [0.60, [0x81, 0x8c, 0xf8]],
    [0.80, [0xf9, 0x73, 0x16]],
    [1.00, [0xf4, 0x3f, 0x5e]],
  ];

  /* Colour band per 10 floors, low -> high. */
  function bandColor(floorNumber, totalFloors) {
    const t = totalFloors > 1 ? (floorNumber - 1) / (totalFloors - 1) : 0;
    let a = BAND_STOPS[0], b = BAND_STOPS[BAND_STOPS.length - 1];
    for (let i = 0; i < BAND_STOPS.length - 1; i++) {
      if (t >= BAND_STOPS[i][0] && t <= BAND_STOPS[i + 1][0]) { a = BAND_STOPS[i]; b = BAND_STOPS[i + 1]; break; }
    }
    const span = (b[0] - a[0]) || 1;
    const k = (t - a[0]) / span;
    const rgb = a[1].map((c, i) => Math.round(c + (b[1][i] - c) * k));
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2];
  }

  /** Project lon/lat ring to local metres centred on the footprint. */
  function projectRing(ring) {
    // Single pass for the extents: Math.min(...lats) spreads the whole ring
    // into the argument list, which is both slower and stack-limited.
    let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
    for (const p of ring) {
      if (p[0] < minLon) minLon = p[0];
      if (p[0] > maxLon) maxLon = p[0];
      if (p[1] < minLat) minLat = p[1];
      if (p[1] > maxLat) maxLat = p[1];
    }
    const lat0 = (minLat + maxLat) / 2;
    const lon0 = (minLon + maxLon) / 2;
    const mLon = 111320 * Math.cos(lat0 * Math.PI / 180);
    return ring.map(([lon, lat]) => [(lon - lon0) * mLon, (lat - lat0) * M_PER_DEG_LAT]);
  }

  /**
   * Slab thickness for a floor, and the top of the model. Pure arithmetic on
   * the payload, so render() can report its stats before the GPU exists.
   */
  function slabThickness(f) { return Math.max(0.6, f.floor_height_m * 0.82); }

  function summarise(floors) {
    let maxTop = 0;
    for (const f of floors) maxTop = Math.max(maxTop, f.base_elevation_m + slabThickness(f));
    return { floors: floors.length || 1, height: maxTop };
  }

  /* ------------------------------ render loop ----------------------------- */

  /**
   * Ask for exactly one frame. Idempotent: a hundred calls in the same tick
   * still paint once.
   */
  function requestRender() {
    if (frameId !== null || !renderer) return;
    frameId = requestAnimationFrame(paint);
  }

  function paint() {
    frameId = null;
    if (!renderer || !scene) return;
    // A closed modal (display:none) has no size; painting it is pure waste.
    if (!container || !container.clientWidth || !container.clientHeight) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    // OrbitControls.update() reports whether damping actually moved the
    // camera, so the loop continues only while the view is still settling and
    // stops the instant it is not.
    const moved = controls ? controls.update() : false;
    renderer.render(scene, camera);
    if (moved) requestRender();
  }

  /* --------------------------------- setup -------------------------------- */

  function setup() {
    const el = container;
    const w = el.clientWidth || 800;
    const h = el.clientHeight || 500;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x080c17);
    scene.fog = new THREE.Fog(0x080c17, 400, 2600);

    camera = new THREE.PerspectiveCamera(50, w / h, 0.5, 12000);
    camera.position.set(160, 130, 200);

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    el.appendChild(renderer.domElement);

    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.07;
    controls.maxPolarAngle = Math.PI * 0.495;   // stay above ground
    // 'change' only fires from update(), i.e. from inside paint(), which is
    // what keeps damping running. The pointer/wheel hooks below are what
    // start a frame in the first place.
    controls.addEventListener('change', requestRender);
    ['pointerdown', 'wheel', 'touchstart'].forEach((t) =>
      renderer.domElement.addEventListener(t, requestRender, { passive: true }));

    scene.add(new THREE.HemisphereLight(0xbfd8ff, 0x0a0f1c, 1.05));
    const key = new THREE.DirectionalLight(0xffffff, 1.15);
    key.position.set(180, 320, 160);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x6366f1, 0.55);
    rim.position.set(-200, 120, -180);
    scene.add(rim);

    raycaster = new THREE.Raycaster();
    pointer = new THREE.Vector2();

    renderer.domElement.addEventListener('pointermove', onPointerMove);
    renderer.domElement.addEventListener('click', onClick);
    window.addEventListener('resize', resize);
    // Repaint when the tab comes back; nothing was drawn while it was hidden.
    document.addEventListener('visibilitychange', () => { if (!document.hidden) requestRender(); });

    if (pending) { const p = pending; pending = null; buildGeometry(p.data, p.ring); }
    requestRender();
  }

  /**
   * Begin loading three.js and build the scene. Safe to call on every modal
   * open: the download and the WebGL context are created once.
   *
   * Note the guard is `started`, not "is anything pending" - render() can
   * legitimately queue work before init() runs, and that must not stop the
   * viewer from ever being built.
   */
  function init(el) {
    if (started) { if (el) container = el; requestRender(); return; }
    started = true;
    container = el;
    ensureThree().then(setup).catch((err) => {
      started = false;              // allow a retry on the next open
      container = null;
      if (typeof UI !== 'undefined' && UI.toast) {
        UI.toast(`3D viewer unavailable (${err.message}). Floor data still works below.`, 'warn', 6000);
      }
    });
  }

  function clearScene() {
    floorMeshes.forEach((m) => {
      scene.remove(m);
      m.geometry.dispose();
      m.material.dispose();
    });
    floorMeshes = [];
    hovered = null;
    // Remove helpers (grid/ground) too.
    [...scene.children].forEach((c) => {
      if (c.userData.isHelper) {
        scene.remove(c);
        c.geometry?.dispose?.();
        c.material?.dispose?.();
      }
    });
  }

  /**
   * @param {object} data  Response from /api/v1/generate-3d-model
   * @param {number[][]} ring  Footprint as [[lon,lat],...]
   * @returns {{floors: number, height: number}} stats for the caller, which
   *          are derived from the payload alone and therefore available even
   *          when three.js is still downloading.
   */
  function render(data, ring) {
    model = data;
    const floors = data.floors || [];
    const stats = summarise(floors);
    if (!renderer) { pending = { data, ring }; return stats; }
    buildGeometry(data, ring);
    return stats;
  }

  function buildGeometry(data, ring) {
    clearScene();

    const pts = projectRing(ring);
    const shape = new THREE.Shape();
    pts.forEach(([x, y], i) => (i === 0 ? shape.moveTo(x, y) : shape.lineTo(x, y)));
    shape.closePath();

    const floors = data.floors || [];
    const total = floors.length || 1;

    // Ground plane + grid for scale.
    let maxAbs = 0;
    for (const [x, y] of pts) {
      const ax = Math.abs(x), ay = Math.abs(y);
      if (ax > maxAbs) maxAbs = ax;
      if (ay > maxAbs) maxAbs = ay;
    }
    const extent = maxAbs * 4 + 60;
    const grid = new THREE.GridHelper(extent * 2, 26, 0x1e293b, 0x131c2e);
    grid.position.y = -0.4;
    grid.userData.isHelper = true;
    scene.add(grid);

    // Triangulate the footprint ONCE at unit height, then clone and stretch it
    // per floor. Extruding each floor separately re-ran earcut over the same
    // polygon once per storey - 163 identical triangulations for a tall tower.
    // Rotating first means the extrusion axis is +Y, so a floor slab is just a
    // Y scale; applyMatrix4 re-derives the normals for us.
    const template = new THREE.ExtrudeGeometry(shape, { depth: 1, bevelEnabled: false });
    template.rotateX(-Math.PI / 2);   // XY shape -> XZ ground plane

    let maxTop = 0;
    floors.forEach((f) => {
      const thickness = slabThickness(f);   // gap reads as a slab line
      const geom = template.clone();
      geom.scale(1, thickness, 1);

      const isMech = f.floor_type === 'mechanical';
      const mat = new THREE.MeshStandardMaterial({
        color: isMech ? 0x64748b : bandColor(f.floor_number, total),
        metalness: 0.25,
        roughness: 0.55,
        transparent: true,
        opacity: isMech ? 0.95 : 0.88,
      });

      const mesh = new THREE.Mesh(geom, mat);
      mesh.position.y = f.base_elevation_m;
      mesh.userData.floor = f;
      mesh.userData.baseColor = mat.color.getHex();
      scene.add(mesh);
      floorMeshes.push(mesh);
      maxTop = Math.max(maxTop, f.base_elevation_m + thickness);
    });
    template.dispose();               // cloned into every floor, never drawn
    if (wantedHighlight !== null) applyHighlight();

    // Frame the whole tower.
    const dist = Math.max(extent * 1.4, maxTop * 1.25, 60);
    camera.position.set(dist * 0.75, Math.max(maxTop * 0.65, 40), dist);
    controls.target.set(0, maxTop * 0.42, 0);
    controls.update();
    requestRender();
  }

  function onPointerMove(e) {
    const r = renderer.domElement.getBoundingClientRect();
    pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;

    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(floorMeshes, false)[0];
    const next = hit ? hit.object : null;

    // Only repaint (and only touch the DOM) when the hovered floor changed.
    // Raycasting 163 meshes on every pointermove is unavoidable while the
    // cursor is over the canvas, but re-rendering is not.
    const changed = next !== hovered;
    if (hovered && changed) {
      hovered.material.emissive?.setHex(0x000000);
      hovered.material.opacity = hovered.userData.floor.floor_type === 'mechanical' ? 0.95 : 0.88;
      hovered = null;
    }
    const tip = document.getElementById('viewer3dTip');
    if (next) {
      hovered = next;
      if (changed) {
        hovered.material.emissive?.setHex(0x2563eb);
        hovered.material.opacity = 1;
      }
      const f = hovered.userData.floor;
      if (tip) {
        tip.innerHTML =
          `<b class="text-sky-300">Floor ${f.floor_number}</b> · ${f.floor_type}` +
          `<br><span class="text-slate-400">${f.units_on_floor} units · ${f.floor_height_m} m ·` +
          ` elev ${f.base_elevation_m} m</span>` +
          `<br><span class="font-mono text-[10px] text-slate-500">${f.floor_ulpin}</span>`;
        tip.classList.remove('hidden');
        tip.style.left = `${e.clientX - r.left + 14}px`;
        tip.style.top = `${e.clientY - r.top + 14}px`;
      }
      renderer.domElement.style.cursor = 'pointer';
    } else {
      tip?.classList.add('hidden');
      renderer.domElement.style.cursor = 'grab';
    }
    if (changed) requestRender();
  }

  function onClick() {
    if (hovered && onFloorPick) onFloorPick(hovered.userData.floor);
  }

  function resize() {
    if (!container || !renderer) return;
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    requestRender();
  }

  function highlightFloor(floorNumber) {
    wantedHighlight = floorNumber;
    applyHighlight();
  }

  function resetHighlight() {
    wantedHighlight = null;
    applyHighlight();
  }

  /**
   * Paint the remembered highlight onto whatever meshes exist. Called again at
   * the end of buildGeometry(): with three.js now loaded on demand, a floor can
   * be selected (a 20-digit ULPIN, a click in the list) before the first mesh
   * has been created, and that choice would otherwise be silently dropped.
   */
  function applyHighlight() {
    const on = wantedHighlight;
    floorMeshes.forEach((m) => {
      const isOn = on !== null && m.userData.floor.floor_number === on;
      m.material.emissive?.setHex(isOn ? 0x22c55e : 0x000000);
      m.material.opacity = isOn ? 1
        : (on !== null ? 0.55
          : (m.userData.floor.floor_type === 'mechanical' ? 0.95 : 0.88));
    });
    requestRender();
  }

  return {
    init, render, resize, highlightFloor, resetHighlight,
    set onFloorPick(fn) { onFloorPick = fn; },
    get model() { return model; },
    /** True once three.js has loaded and the WebGL context exists. */
    get ready() { return !!renderer; },
    dispose() {
      if (frameId !== null) { cancelAnimationFrame(frameId); frameId = null; }
      pending = null;
      started = false;
      wantedHighlight = null;
      if (scene) clearScene();
      renderer?.dispose();
      renderer = null;
    },
  };
})();

window.Viewer3D = Viewer3D;
