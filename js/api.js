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
    let configured = (typeof window !== 'undefined' && window.API_BASE_URL) || '';
    configured = configured.trim().replace(/\/+$/, '');

    // When the page is already served by the backend, always talk to our own
    // origin and ignore the configured URL. Beyond being redundant, an
    // absolute cross-origin URL here would be a third-party request, which is
    // exactly what tracking prevention cancels - the reason this same-origin
    // deployment exists. Same origin also skips CORS entirely.
    if (typeof location !== 'undefined' && configured) {
      try {
        if (new URL(configured).origin === location.origin) return '';
      } catch (_) { /* not an absolute URL; fall through */ }
    }

    if (configured) {
      // A page served over https cannot call an http API - the browser blocks
      // it as mixed content. Upgrade rather than fail silently. localhost is
      // exempt: browsers treat it as a secure context, and it has no https
      // listener to upgrade to.
      const isLoopback = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|$)/i.test(configured);
      if (location.protocol === 'https:' && configured.startsWith('http://') && !isLoopback) {
        configured = 'https://' + configured.slice('http://'.length);
      }
      return configured;
    }
    if (['localhost', '127.0.0.1'].includes(location.hostname)) return 'http://127.0.0.1:8000';
    return '';
  }

  let BASE = resolveBase();
  let online = false;
  let lastFailure = null;

  // Health-probe paths, tried in order.
  //
  // "/health" is a token ad-block and tracking-prevention filter lists match,
  // so it can be cancelled locally (ERR_BLOCKED_BY_CLIENT) even when the API
  // is perfectly healthy. The rest are ordinary-looking paths returning the
  // same information, so a list matching one token cannot take the status
  // check down with it.
  //
  // "/" is last on purpose but matters most: it is the API root, it exists on
  // every version of this backend ever deployed, and it needs no redeploy to
  // start working. It reports liveness only - it has no database field - so
  // it is a fallback, not a replacement.
  const HEALTH_PATHS = ['/health', '/status', '/ulpin-status', '/'];
  let healthPath = HEALTH_PATHS[0];

  /** Normalise a probe response: "/" returns the API banner, not a health doc. */
  function asHealth(body, path) {
    if (path !== '/') return body;
    const d = (body && body.data) || {};
    return { status: 'ok', version: d.version || '', database: 'unknown', parcels: null };
  }

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
      //
      // Try each probe path in turn. "/health" is a token many ad-block and
      // tracking-prevention filter lists match, so when it is cancelled with
      // ERR_BLOCKED_BY_CLIENT the identical aliases below usually sail
      // through - the request is only being judged on its URL.
      let blockedAll = true;
      for (const path of HEALTH_PATHS) {
        const started = Date.now();
        try {
          const r = asHealth(await request(path, {}, 8000), path);
          online = r.status === 'ok';
          healthPath = path;
          return r;
        } catch (err) {
          // A request killed by an ad blocker, privacy extension or tracking
          // prevention never reaches the network: it rejects with a TypeError
          // almost instantly. A sleeping host behaves the opposite way - it
          // hangs. Use that to tell them apart, because retrying for 75s
          // cannot help a blocked request and reporting "offline" sends
          // people to debug a server that is perfectly fine.
          if (!isLikelyBlocked(err, Date.now() - started)) blockedAll = false;
          // Keep trying the remaining paths either way: a 404 just means this
          // deployment predates that alias, and a timeout on one path says
          // nothing about whether another is blocked.
        }
      }
      // Every path was cancelled locally: this is definitely a client-side
      // blocker, and no amount of retrying will change that.
      if (blockedAll) { lastFailure = 'blocked'; online = false; return null; }

      // Nothing to wake if no backend is configured at all.
      if (!BASE) { lastFailure = 'unconfigured'; online = false; return null; }

      if (typeof onWaking === 'function') { try { onWaking(); } catch (_) {} }

      // Slow path: cold start. Poll until the service answers or we give up.
      const deadline = Date.now() + 75000;
      let blockedStreak = 0;
      while (Date.now() < deadline) {
        // Re-probe every path, not just the first. A waking host may answer
        // one path before another, and one of these may be the only one the
        // browser is willing to send.
        let sawReal = false;
        for (const path of HEALTH_PATHS) {
          const attempt = Date.now();
          try {
            const r = asHealth(await request(path, {}, 20000), path);
            online = r.status === 'ok';
            healthPath = path;
            return r;
          } catch (err) {
            if (!isLikelyBlocked(err, Date.now() - attempt)) sawReal = true;
          }
        }
        // Consistent instant rejections across every path mean a client-side
        // blocker, not a cold start. Bail out rather than spin for the budget.
        if (!sawReal) {
          if (++blockedStreak >= 2) {
            lastFailure = 'blocked';
            online = false;
            return null;
          }
        } else {
          blockedStreak = 0;
        }
        await new Promise(res => setTimeout(res, 2500));
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
