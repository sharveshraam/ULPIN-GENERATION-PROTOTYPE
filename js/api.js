/**
 * Backend API client.
 *
 * The base URL comes from ONE place only: API_BASE_URL in js/config.js.
 * There is deliberately no query-parameter or localStorage override — the
 * endpoint is set in code, so what is deployed is exactly what runs.
 *
 * If API_BASE_URL is left empty the client falls back to localhost during
 * local development, and otherwise to same-origin.
 *
 * Every call degrades gracefully: if the backend is unreachable the UI falls
 * back to client-side computation so the app still works.
 */
const API = (() => {

  function resolveBase() {
    const configured = (typeof window !== 'undefined' && window.API_BASE_URL) || '';
    if (configured) return configured.replace(/\/$/, '');
    if (['localhost', '127.0.0.1'].includes(location.hostname)) return 'http://127.0.0.1:8000';
    return '';
  }

  let BASE = resolveBase();
  let online = false;

  const url = (path) => `${BASE}${path}`;

  async function request(path, options = {}, timeoutMs = 120000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      // Only send Content-Type when there is a body. On a GET it is a
      // non-simple header that forces a CORS preflight for no reason, and a
      // backend whose ALLOWED_ORIGINS does not list this exact origin will
      // reject that OPTIONS with a 400 - even though the plain GET succeeds.
      // That combination looks like "backend offline" while /health opens
      // perfectly in a browser tab.
      const headers = { ...(options.headers || {}) };
      if (options.body != null && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
      }
      const res = await fetch(url(path), {
        ...options,
        signal: ctrl.signal,
        headers,
      });
      const text = await res.text();
      let body;
      try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }

      if (!res.ok) {
        // FastAPI puts validation errors in `detail`.
        const msg = body.detail || body.message ||
          (Array.isArray(body) ? body.map(e => e.msg).join(', ') : `HTTP ${res.status}`);
        throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      }
      return body;
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    /** Read-only: the endpoint is fixed in js/config.js, not changeable at runtime. */
    get base() { return BASE; },
    get isOnline() { return online; },

    /**
     * Probe /health so the UI can show an accurate status pill.
     *
     * Free-tier hosts (Render, Fly, etc.) suspend idle services, and the first
     * request then takes ~50s while the container wakes. A short timeout makes
     * a perfectly healthy backend look offline, so this retries with a
     * generous budget before giving up.
     *
     * @param {function} onWaking called once if the first quick probe fails,
     *                            so the UI can say "waking backend…"
     */
    async checkHealth(onWaking) {
      // Fast path: a warm backend answers well inside this.
      try {
        const r = await request('/health', {}, 8000);
        online = r.status === 'ok';
        return r;
      } catch (_) { /* fall through to the slow path */ }

      // Nothing to wake if no backend is configured at all.
      if (!BASE) { online = false; return null; }

      if (typeof onWaking === 'function') { try { onWaking(); } catch (_) {} }

      // Slow path: cold start. Poll until the service answers or we give up.
      const deadline = Date.now() + 75000;
      while (Date.now() < deadline) {
        try {
          const r = await request('/health', {}, 20000);
          online = r.status === 'ok';
          return r;
        } catch (_) {
          await new Promise(res => setTimeout(res, 2500));
        }
      }
      online = false;
      return null;
    },

    generateUlpin: (payload) =>
      request('/api/v1/generate-ulpin', { method: 'POST', body: JSON.stringify(payload) }),

    decodeUlpin: (ulpin) => request(`/api/v1/decode-ulpin/${encodeURIComponent(ulpin)}`),

    listParcels: (limit = 1000) => request(`/api/v1/parcels?limit=${limit}`),

    getParcel: (ulpin) => request(`/api/v1/parcels/${encodeURIComponent(ulpin)}`),

    createParcel: (payload) =>
      request('/api/v1/parcels', { method: 'POST', body: JSON.stringify(payload) }),

    deleteParcel: (ulpin) =>
      request(`/api/v1/parcels/${encodeURIComponent(ulpin)}`, { method: 'DELETE' }),

    getFloors: (ulpin) => request(`/api/v1/parcels/${encodeURIComponent(ulpin)}/floors`),

    getUnits: (ulpin, { floor = null, limit = 500, offset = 0 } = {}) => {
      const qs = new URLSearchParams({ limit, offset });
      if (floor != null) qs.set('floor', floor);
      return request(`/api/v1/parcels/${encodeURIComponent(ulpin)}/units?${qs}`);
    },

    generate3DModel: (payload) =>
      request('/api/v1/generate-3d-model', { method: 'POST', body: JSON.stringify(payload) }),

    /** Bulk-generate ULPINs for every building within a radius. */
    bulkGenerate: ({ lat, lon, radiusKm = 1.0, persist = true, breakdown = false }) =>
      request('/api/v1/bulk-generate', {
        method: 'POST',
        body: JSON.stringify({
          center_lat: lat, center_lon: lon, radius_km: radiusKm,
          persist, generate_breakdown: breakdown,
        }),
      }),

    search: (q) => request(`/api/v1/search?q=${encodeURIComponent(q)}`),
  };
})();

window.API = API;
