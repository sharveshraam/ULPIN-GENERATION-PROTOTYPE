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
      const res = await fetch(url(path), {
        ...options,
        signal: ctrl.signal,
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
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

    /** Probe /health so the UI can show an accurate status pill. */
    async checkHealth() {
      try {
        const r = await request('/health', {}, 6000);
        online = r.status === 'ok';
        return r;
      } catch {
        online = false;
        return null;
      }
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
