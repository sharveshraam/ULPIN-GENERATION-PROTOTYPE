/**
 * Cinematic "descend from orbit" transition for the map.
 *
 * When a location or ULPIN is searched, instead of a flat pan we:
 *   1. pull UP to a wide/orbital zoom (so you see where you are on Earth),
 *   2. travel across to the destination at that altitude,
 *   3. descend onto the target,
 * with a HUD overlay showing the target coordinates counting up to their
 * final values, mirroring the feel of the 3D model generation.
 *
 * Leaflet's flyTo already interpolates zoom and centre together along a
 * smooth curve; chaining two legs (out-and-across, then down) is what gives
 * the sense of leaving the surface and arriving somewhere new.
 *
 * Everything here is presentation only. If anything goes wrong the map is
 * still moved to the destination — the animation can never strand the user
 * somewhere else.
 */
const FlyTo = (() => {
  let overlay = null, busy = false;

  /** Build the HUD once, lazily. */
  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'flyHud';
    overlay.className = 'fly-hud hidden';
    overlay.innerHTML = `
      <div class="fly-reticle">
        <span class="fly-ring"></span>
        <span class="fly-ring fly-ring-2"></span>
        <span class="fly-cross"></span>
      </div>
      <div class="fly-readout">
        <div class="fly-label" id="flyLabel">Locating</div>
        <div class="fly-coords"><span id="flyLat">0.0000</span>, <span id="flyLon">0.0000</span></div>
        <div class="fly-sub" id="flySub">Descending…</div>
      </div>`;
    // The HUD is absolutely positioned, so it must live inside the map's
    // positioned ancestor — appending to <body> would anchor it to the page
    // instead of the map viewport.
    const mapEl = document.getElementById('map');
    const container = (mapEl && mapEl.parentElement) || document.body;
    container.appendChild(overlay);
    return overlay;
  }

  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  /** Animate the coordinate readout from the current view to the target. */
  function runReadout(fromLat, fromLon, toLat, toLon, ms) {
    const latEl = document.getElementById('flyLat');
    const lonEl = document.getElementById('flyLon');
    const t0 = performance.now();
    return new Promise((resolve) => {
      const step = (now) => {
        const p = Math.min(1, (now - t0) / ms);
        const e = easeOut(p);
        latEl.textContent = (fromLat + (toLat - fromLat) * e).toFixed(4);
        lonEl.textContent = (fromLon + (toLon - fromLon) * e).toFixed(4);
        if (p < 1) requestAnimationFrame(step); else resolve();
      };
      requestAnimationFrame(step);
    });
  }

  /** Resolve when the map stops moving, or after a hard timeout. */
  function whenIdle(map, timeoutMs) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        map.off('moveend', finish);
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(finish, timeoutMs);
      map.on('moveend', finish);
    });
  }

  /**
   * Fly to [lat, lon] with the orbital transition.
   *
   * @param {L.Map}  map
   * @param {number} lat
   * @param {number} lon
   * @param {object} opts  { zoom, label, sub, orbitZoom }
   */
  async function to(map, lat, lon, opts = {}) {
    const zoom      = opts.zoom      ?? 18;
    const label     = opts.label     ?? 'Target acquired';
    const sub       = opts.sub       ?? 'Descending to surface…';
    const from      = map.getCenter();
    const startZoom = map.getZoom();

    // Respect reduced-motion and avoid overlapping animations: jump directly.
    const reduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced || busy) {
      map.setView([lat, lon], zoom);
      return;
    }

    busy = true;
    const hud = ensureOverlay();
    document.getElementById('flyLabel').textContent = label;
    document.getElementById('flySub').textContent = sub;
    document.getElementById('flyLat').textContent = from.lat.toFixed(4);
    document.getElementById('flyLon').textContent = from.lng.toFixed(4);
    hud.classList.remove('hidden');
    requestAnimationFrame(() => hud.classList.add('fly-on'));

    try {
      // How far are we going? A short hop should not pretend to orbit.
      const dist = map.distance([from.lat, from.lng], [lat, lon]); // metres
      const far  = dist > 8000;

      // Altitude for the cruise leg: further away -> higher up.
      const orbitZoom = opts.orbitZoom ??
        (dist > 1.5e6 ? 4 : dist > 4e5 ? 6 : dist > 5e4 ? 9 : 12);

      if (far) {
        // Leg 1 — climb and cross at altitude.
        map.flyTo([lat, lon], Math.min(orbitZoom, startZoom), {
          duration: 1.15, easeLinearity: 0.25,
        });
        await whenIdle(map, 1500);

        // Leg 2 — descend onto the target.
        document.getElementById('flySub').textContent = 'Descending to surface…';
        map.flyTo([lat, lon], zoom, { duration: 1.5, easeLinearity: 0.2 });
        runReadout(from.lat, from.lng, lat, lon, 1400);
        await whenIdle(map, 1900);
      } else {
        // Close by: a single smooth descent reads better than a fake orbit.
        map.flyTo([lat, lon], zoom, { duration: 1.25, easeLinearity: 0.25 });
        runReadout(from.lat, from.lng, lat, lon, 1150);
        await whenIdle(map, 1600);
      }
    } catch (_) {
      map.setView([lat, lon], zoom);      // never strand the user
    } finally {
      document.getElementById('flyLat').textContent = lat.toFixed(4);
      document.getElementById('flyLon').textContent = lon.toFixed(4);
      hud.classList.remove('fly-on');
      setTimeout(() => hud.classList.add('hidden'), 420);
      busy = false;
    }
  }

  return { to, get busy() { return busy; } };
})();

window.FlyTo = FlyTo;
