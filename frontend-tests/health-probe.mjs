/**
 * Regression tests for the health probe and endpoint selection in js/api.js.
 *
 *   node frontend-tests/health-probe.mjs
 *
 * No dependencies, no browser. js/api.js is evaluated inside a Function with
 * hand-made `fetch`, `sessionStorage`, `AbortController` and `Date` stubs plus
 * a controllable fake clock, so a 75-second cold-start budget runs in
 * milliseconds and the exact requests it produces are countable.
 *
 * The interesting variable is ORIGIN. A page can be served by the backend
 * itself (Render's /app/, a local uvicorn, a preview) or by something else
 * entirely (GitHub Pages), while js/config.js pins one absolute deployment
 * URL. These tests pin down which server the client ends up talking to, and
 * how much load it puts on a host that is asleep or starved of CPU.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const API_JS = path.join(ROOT, 'js', 'api.js');
const HEALTH = JSON.stringify({ status: 'ok', version: '1.0', database: 'sqlite', parcels: 7 });

const RENDER = 'https://ulpin-generation-prototype.onrender.com';   // what config.js pins
const PREVIEW = 'https://8000-sandbox.e2b.app';                     // a page served elsewhere
const PAGES = 'https://sharveshraam.github.io';                     // static host, no API
// GitHub Pages serves the repo root, so the map page is NOT under /app/. That
// difference is what lets resolveBase() decide same-origin synchronously.
const PAGES_PATH = '/ULPIN-GENERATION-PROTOTYPE/map.html';

/* ------------------------------- fake browser ---------------------------- */

const RealDate = Date;

/**
 * @param {object} opts
 * @param {string} opts.pageOrigin   origin the page itself was served from
 * @param {string} [opts.apiBaseUrl] js/config.js constant (defaults to RENDER)
 * @param {(url:string, n:number)=>object} opts.behaviour  what each request does
 * @param {Record<string,string>} [opts.session]  pre-seeded sessionStorage
 *
 * `url` is exactly what fetch() received: a bare path for same-origin calls,
 * an absolute URL for cross-origin ones. behaviour returns any of:
 *   { blocked: true }        reject instantly with a TypeError (extension kill)
 *   { ms, status?, body? }   answer after `ms` with an HTTP response
 *   { ms, abort: true }      hang for `ms`; the client's own timeout wins if shorter
 */
function makeEnv({ pageOrigin, pathname = '/app/map.html', apiBaseUrl = RENDER, behaviour, session = {} }) {
  let now = 0, idc = 0, lastTimerMs = 0;
  const timers = new Map();
  const store = { ...session };
  const calls = [];

  const hostname = pageOrigin.replace(/^https?:\/\//, '').split(':')[0];
  const location = { origin: pageOrigin, protocol: 'https:', hostname, pathname };

  const g = {
    Date: class extends RealDate { static now() { return now; } },
    setTimeout(fn, ms = 0) { const id = ++idc; lastTimerMs = ms; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeout(id) { timers.delete(id); },
    AbortController: class {
      constructor() { this.signal = { aborted: false }; }
      abort() { this.signal.aborted = true; }
    },
    URL, console, location,
    sessionStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
  };
  g.window = { API_BASE_URL: apiBaseUrl, location, matchMedia: () => ({ matches: false }) };

  g.fetch = async (u) => {
    const url = String(u);
    calls.push(url);
    const beh = behaviour(url, calls.length) || { ms: 0 };
    if (beh.blocked) throw new TypeError('Failed to fetch');   // never advances the clock
    // request() schedules its abort timer immediately before calling fetch, so
    // lastTimerMs is this request's timeout. A request cannot outlive it.
    const timeout = lastTimerMs || 20000;
    const lat = beh.ms || 0;
    now += Math.min(lat, timeout);
    if (beh.abort || lat >= timeout) { const e = new Error('aborted'); e.name = 'AbortError'; throw e; }
    const status = beh.status || 200;
    return { ok: status < 400, status, text: async () => (beh.body !== undefined ? beh.body : HEALTH) };
  };

  const src = fs.readFileSync(API_JS, 'utf8');
  const API = new Function(...Object.keys(g), `${src}; return API;`)(...Object.values(g));

  /** Run the clock forward to whichever timer is due next. */
  async function tick() {
    await Promise.resolve();
    if (!timers.size) return;
    let next = Infinity;
    for (const t of timers.values()) next = Math.min(next, t.at);
    now = Math.max(now, next);
    for (;;) {
      let best = null;
      for (const [id, t] of timers) if (t.at <= now && (!best || t.at < best[1].at)) best = [id, t];
      if (!best) return;
      timers.delete(best[0]);
      best[1].fn();
      await Promise.resolve();
    }
  }

  async function checkHealth() {
    const p = API.checkHealth(() => {});
    for (let i = 0; i < 200000; i++) {
      if (await Promise.race([p.then(() => true, () => true), Promise.resolve(false)])) break;
      await tick();
    }
    const res = await p;
    return { res, requests: calls.length, calls: calls.slice(), elapsedMs: now,
             online: API.isOnline, failure: API.lastFailure, base: API.base, session: { ...store } };
  }

  return { API, calls, checkHealth };
}

/* --------------------------------- runner -------------------------------- */

let failures = 0, passed = 0;
function check(name, cond, detail = '') {
  if (cond) { passed++; console.log(`  ok    ${name}`); }
  else { failures++; console.log(`  FAIL  ${name}${detail ? `  -- ${detail}` : ''}`); }
}

const UP = { ms: 2 };
const HANG = { ms: 60000, abort: true };
const NOT_FOUND = { ms: 3, status: 404, body: '<html>404</html>' };
const SLOW_HEALTHY = { ms: 6000 };
const STARTING = { ms: 5000, body: JSON.stringify({ detail: 'starting up' }) };

const isSame = (u) => !/^https?:/.test(u);            // relative => our own origin
const isRemote = (u) => u.startsWith(RENDER);

console.log('\njs/api.js checkHealth() — which server does the page talk to?');

{
  // THE BUG: the page is served by the backend, so BASE resolves to '' and the
  // probe used to bail out as 'unconfigured'. The pill then read "Browser mode"
  // on a deployment whose own server was serving the page.
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: () => UP }).checkHealth();
  check('Render /app/, warm: reports live (was "Browser mode")', r.online === true);
  check('Render /app/, warm: not diagnosed unconfigured', r.failure === null, String(r.failure));
  check('Render /app/, warm: one same-origin request', r.requests === 1 && isSame(r.calls[0]), JSON.stringify(r.calls));
}

{
  // A preview / local dev / review deployment: the page is served by one
  // backend while config.js points at production. Trust the server that served
  // the page.
  const r = await makeEnv({ pageOrigin: PREVIEW, behaviour: (u) => (isSame(u) ? UP : HANG) }).checkHealth();
  check('preview: talks to the server that served it', r.online === true);
  check('preview: switches BASE to same origin', r.base === '', JSON.stringify(r.base));
  check('preview: never called production', !r.calls.some(isRemote), JSON.stringify(r.calls));
}

{
  // Same, but the local server is asleep: prefer it and wake it rather than
  // silently falling back to production data.
  const r = await makeEnv({ pageOrigin: PREVIEW, behaviour: (u) => (isSame(u) ? HANG : UP) }).checkHealth();
  check('preview, own server asleep: stays same-origin', r.base === '');
  check('preview, own server asleep: does not use production', !r.calls.some(isRemote));
  check('preview, own server asleep: enters the cold-start wait', r.elapsedMs > 4000);
}

{
  // GitHub Pages: no API here, so the configured URL is the right answer.
  const r = await makeEnv({ pageOrigin: PAGES, pathname: PAGES_PATH,
                            behaviour: (u) => (isSame(u) ? NOT_FOUND : UP) }).checkHealth();
  check('GitHub Pages: falls back to the configured URL', r.online === true);
  check('GitHub Pages: keeps the cross-origin base', r.base === RENDER, JSON.stringify(r.base));
  check('GitHub Pages: gives up on same origin quickly', r.elapsedMs < 20000, `${r.elapsedMs}ms`);
}

{
  // Static host AND a production backend that answers 404: nothing is asleep,
  // so do not spend 75 s polling something that replies instantly.
  const r = await makeEnv({ pageOrigin: PAGES, pathname: PAGES_PATH,
                            behaviour: () => NOT_FOUND }).checkHealth();
  check('no API anywhere: reports unreachable', r.online === false && r.failure === 'unreachable');
  check('no API anywhere: fails fast, no 75 s poll', r.elapsedMs < 20000, `${(r.elapsedMs / 1000).toFixed(1)}s`);
}

{
  // The /app/ rule must fire synchronously, before any probe: a click during a
  // slow health check has to reach the server that served the page.
  const r = await makeEnv({ pageOrigin: PREVIEW, behaviour: (u) => (isSame(u) ? UP : HANG) }).checkHealth();
  check('/app/ page: base is same-origin before probing', r.base === '', JSON.stringify(r.base));
  check('/app/ page: no cross-origin request at all', !r.calls.some(isRemote), JSON.stringify(r.calls));

  const root = await makeEnv({ pageOrigin: PREVIEW, pathname: '/',
                               behaviour: (u) => (isSame(u) ? UP : HANG) }).checkHealth();
  check('root-mounted page: probe still finds same origin', root.online === true && root.base === '');
}

console.log('\njs/api.js checkHealth() — load on the host');

{
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: () => UP }).checkHealth();
  check('warm host: one request only', r.requests === 1, `${r.requests} req`);
  check('warm host: remembers the path', r.session['ulpin.healthPath'] === '/health');
}

{
  const blocked = (u) => (u.endsWith('/health') ? { blocked: true } : UP);
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: blocked }).checkHealth();
  check('blocked /health: falls through to /status', r.online === true);
  check('blocked /health: remembers /status', r.session['ulpin.healthPath'] === '/status');

  // The point of remembering it: no hang on a path this browser will never send.
  const r2 = await makeEnv({ pageOrigin: RENDER, behaviour: blocked,
                             session: { 'ulpin.healthPath': '/status' } }).checkHealth();
  check('returning visitor: one request', r2.requests === 1, `${r2.requests} req`);
  check('returning visitor: no time lost', r2.elapsedMs < 1000, `${r2.elapsedMs}ms`);
  check('returning visitor: still live', r2.online === true);
}

{
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: () => HANG }).checkHealth();
  check('asleep host: gives up, reports unreachable', r.online === false && r.failure === 'unreachable');
  check('asleep host: stays inside the budget', r.elapsedMs <= 130000, `${(r.elapsedMs / 1000).toFixed(0)}s`);
  check('asleep host: does not hammer', r.requests <= 12, `${r.requests} requests`);
}

{
  const r = await makeEnv({ pageOrigin: RENDER,
    behaviour: (u, n) => (n < 4 ? HANG : UP) }).checkHealth();
  check('waking host: recovers and reports live', r.online === true);
}

{
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: () => ({ blocked: true }) }).checkHealth();
  check('everything blocked: diagnosed as blocked', r.online === false && r.failure === 'blocked');
  check('everything blocked: gives up fast', r.elapsedMs < 20000, `${(r.elapsedMs / 1000).toFixed(0)}s`);
}

{
  // A healthy host that is simply slow. This pins the warm timeout: shrink it
  // below the host's response time and every probe aborts, the sweep burns four
  // requests instead of one, and the pill takes ~20 s to say "live".
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: () => SLOW_HEALTHY }).checkHealth();
  check('slow healthy host: reports live', r.online === true);
  check('slow healthy host: first probe succeeds', r.requests === 1, `${r.requests} req`);
  check('slow healthy host: no retry storm', r.elapsedMs < 10000, `${(r.elapsedMs / 1000).toFixed(1)}s`);
}

{
  // Alive but starved: every request costs the container 5 s of its 0.1 CPU.
  const r = await makeEnv({ pageOrigin: RENDER, behaviour: () => STARTING }).checkHealth();
  check('starved host: not diagnosed as blocked', r.failure === null, String(r.failure));
  check('starved host: bounded request count', r.requests <= 8, `${r.requests} requests`);
}

console.log(`\n${passed} passed, ${failures} failed\n`);
process.exit(failures ? 1 : 0);
