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

  // Warm-pass timeout. A healthy host answers /health in ~2 ms, but a starved
  // 0.1 CPU container can legitimately take seconds, and a tight budget here
  // reads as "offline" on a server that is merely busy - it also stops the
  // sweep from ever reaching the "/" fallback. Keep it generous.
  const WARM_PROBE_TIMEOUT_MS = 8000;
  const COLD_START_BUDGET_MS = 75000;
  const HEALTH_PATH_KEY = 'ulpin.healthPath';

  // Last path that actually answered. Remembered per tab so a returning
  // visitor probes ONE endpoint instead of rediscovering which of the four
  // an ad-blocker happens to allow.
  let healthPath = null;

  function rememberHealthPath(path) {
    healthPath = path;
    try { sessionStorage.setItem(HEALTH_PATH_KEY, path); } catch (_) { /* private mode */ }
  }

  try {
    const saved = sessionStorage.getItem(HEALTH_PATH_KEY);
    if (saved && HEALTH_PATHS.includes(saved)) healthPath = saved;
  } catch (_) { /* private mode */ }

  /** Probe order: whatever worked last time first, then the rest. */
  function probeOrder() {
    return healthPath
      ? [healthPath, ...HEALTH_PATHS.filter((p) => p !== healthPath)]
      : HEALTH_PATHS;
  }

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

  /**
   * Separates "the server answered but CORS hid the response from JS" from
   * "the request never reached the server" (ad blocker, tracking prevention,
   * DNS/network failure, host down).
   *
   * A plain fetch() rejects with a TypeError in BOTH cases, which is why the
   * health check cannot tell them apart from the error alone. A `no-cors`
   * fetch resolves with an opaque response as soon as the server returns ANY
   * HTTP response - CORS headers are irrelevant to it - so:
   *
   *   no-cors resolves  => server is up, plain fetch was CORS-blocked
   *   no-cors rejects   => the request never left / never got answered
   *
   * This is exactly the situation behind the "API blocked" pill appearing
   * even in a private window: the backend runs fine, but its deployed
   * ALLOWED_ORIGINS does not match the page origin, so the browser throws
   * "Failed to fetch" and the frontend misreads it as an ad blocker.
   */
  async function serverIsReachable(timeoutMs = 6000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      await fetch(url('/health'), {
        method: 'GET',
        mode: 'no-cors',
        cache: 'no-store',
        signal: ctrl.signal,
      });
      return true;
    } catch (_) {
      return false;
    } finally {
      clearTimeout(timer);
    }
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

      // Nothing to wake if no backend is configured at all.
      if (!BASE) { lastFailure = 'unconfigured'; online = false; return null; }

      // Fast path: a warm backend answers well inside this.
      //
      // Try each probe path in turn. "/health" is a token many ad-block and
      // tracking-prevention filter lists match, so when it is cancelled with
      // ERR_BLOCKED_BY_CLIENT the identical aliases below usually sail
      // through - the request is only being judged on its URL.
      let blockedAll = true;
      let anyResponse = false;
      for (const path of probeOrder()) {
        const started = Date.now();
        try {
          const r = asHealth(await request(path, {}, WARM_PROBE_TIMEOUT_MS), path);
          anyResponse = true;
          if (r.status === 'ok') {
            online = true;
            rememberHealthPath(path);
            return r;
          }
          // Transport succeeded but the body is not a health payload (e.g. a
          // host splash page). Do NOT report live from that; keep probing.
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

      // Every path failed fast at the network level. It can still be either a
      // client-side blocker OR CORS: both reject as a bare TypeError before
      // JS ever sees a status. Probe with mode:'no-cors' - that request only
      // cares whether the server answered, so it tells the two apart.
      if (blockedAll && !anyResponse) {
        lastFailure = (await serverIsReachable()) ? 'cors' : 'blocked';
        online = false;
        return null;
      }

      if (typeof onWaking === 'function') { try { onWaking(); } catch (_) {} }

      // Slow path: cold start. Poll until the service answers or we give up.
      //
      // Two things keep this off the host's back:
      //   - the interval grows 2.5 -> 4 -> 6.4 -> 8 s instead of staying
      //     pinned at 2.5 s, and each probe is bounded by what is left of the
      //     budget so the loop cannot overshoot it by a whole timeout;
      //   - once an endpoint is known to work, only that one is probed rather
      //     than the whole list on every pass.
      //
      // The sweep is still a full sweep when nothing is remembered: a waking
      // host may answer "/" before "/health", and one of these paths may be
      // the only one an ad-blocker lets through. Dropping it cost a host that
      // answers every request but not with a health document its "/" fallback,
      // which is why it stays.
      //
      // Measured with a fake clock (see tests/test_frontend_health.mjs): a
      // first-time visitor against a sleeping host issues the same number of
      // requests either way, because asHealth() reports "/" as live and ends
      // the loop after one pass. The remembered path is what pays - a repeat
      // visitor whose /health is ad-blocked goes from 2 requests and an 8 s
      // hang to 1 request and none.
      const deadline = Date.now() + COLD_START_BUDGET_MS;
      let delay = 2500;
      let blockedStreak = 0;
      while (true) {
        // Bound each probe by what is left of the budget, so the loop cannot
        // overshoot it by a full timeout on the way out.
        const remaining = deadline - Date.now();
        if (remaining < 1000) break;
        const timeout = Math.min(20000, remaining);
        const paths = healthPath ? [healthPath] : HEALTH_PATHS;
        let sawReal = false;
        for (const path of paths) {
          const attempt = Date.now();
          try {
            const r = asHealth(await request(path, {}, timeout), path);
            if (r.status === 'ok') {
              online = true;
              rememberHealthPath(path);
              return r;
            }
            sawReal = true;   // got an HTTP response; server is alive
          } catch (err) {
            if (!isLikelyBlocked(err, Date.now() - attempt)) sawReal = true;
          }
        }
        // Consistent instant rejections mean a client-side blocker (or CORS),
        // not a cold start. Bail out rather than spin for the whole budget.
        if (!sawReal) {
          if (++blockedStreak >= 2) {
            lastFailure = (await serverIsReachable()) ? 'cors' : 'blocked';
            online = false;
            return null;
          }
        } else {
          blockedStreak = 0;
        }
        await new Promise(res => setTimeout(res, delay));
        delay = Math.min(delay * 1.6, 8000);
      }
      lastFailure = (await serverIsReachable()) ? 'cors' : 'unreachable';
      online = false;
      return null;
    },

    /**
     * Why the last checkHealth() failed:
     *   'cors'         - the server answered but sent no CORS header for this
     *                    origin, so the browser hid the response. Fix the
     *                    backend's ALLOWED_ORIGINS or open /app/ (same origin).
     *   'blocked'      - an extension / tracking prevention cancelled the
     *                    request before it left the browser.
     *   'unreachable'  - the server did not answer (cold start, down, DNS).
     *   'unconfigured' - no API_BASE_URL and not localhost.
     *   null           - checkHealth succeeded.
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
