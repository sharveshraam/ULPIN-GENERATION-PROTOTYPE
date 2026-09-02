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
  let lastFailure = null;

  const url = (path) => `${BASE}${path}`;

  /**
   * Distinguish "a browser extension cancelled this" from "the server is slow
   * or down". Blocked requests reject as a bare TypeError ("Failed to fetch")
   * with no HTTP status, and do so far faster than any real network round
   * trip. An aborted timeout is explicitly excluded - that one means slow.
   *
   * @param {Error} err  the rejection from fetch()
   * @param {number} ms  how long the attempt took
   */
  function isLikelyBlocked(err, ms) {
    if (!err || err.name === 'AbortError') return false;   // timed out => slow, not blocked
    const networkLevel = err instanceof TypeError ||
      /failed to fetch|networkerror|load failed|blocked/i.test(err.message || '');
    return networkLevel && ms < 1500;
  }

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
      lastFailure = null;

      // Fast path: a warm backend answers well inside this.
      const started = Date.now();
      try {
        const r = await request('/health', {}, 8000);
        online = r.status === 'ok';
        return r;
      } catch (err) {
        // A request killed by an ad blocker, privacy extension or tracking
        // prevention never reaches the network: it rejects with a TypeError
        // almost instantly (ERR_BLOCKED_BY_CLIENT). A sleeping host behaves
        // the opposite way - it hangs. Use that to tell them apart, because
        // retrying for 75s cannot help a blocked request and reporting
        // "offline" sends people to debug a server that is perfectly fine.
        if (isLikelyBlocked(err, Date.now() - started)) {
          lastFailure = 'blocked';
          online = false;
          return null;
        }
      }

      // Nothing to wake if no backend is configured at all.
      if (!BASE) { lastFailure = 'unconfigured'; online = false; return null; }

      if (typeof onWaking === 'function') { try { onWaking(); } catch (_) {} }

      // Slow path: cold start. Poll until the service answers or we give up.
      const deadline = Date.now() + 75000;
      let blockedStreak = 0;
      while (Date.now() < deadline) {
        const attempt = Date.now();
        try {
          const r = await request('/health', {}, 20000);
          online = r.status === 'ok';
          return r;
        } catch (err) {
          // Consistent instant rejections mean a client-side blocker, not a
          // cold start. Bail out early rather than spinning for the full budget.
          if (isLikelyBlocked(err, Date.now() - attempt)) {
            if (++blockedStreak >= 3) {
              lastFailure = 'blocked';
              online = false;
              return null;
            }
          } else {
            blockedStreak = 0;
          }
          await new Promise(res => setTimeout(res, 2500));
        }
      }
      lastFailure = 'unreachable';
      online = false;
      return null;
    },

    /**
     * Why the last checkHealth() failed: 'blocked' (an extension or tracking
     * prevention cancelled the request before it left the browser),
     * 'unreachable', 'unconfigured', or null when it succeeded.
     */
    get lastFailure() { return lastFailure; },

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
