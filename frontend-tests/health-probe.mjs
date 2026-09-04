/**
 * Regression test for the health probe in js/api.js.
 *
 *   node frontend-tests/health-probe.mjs
 *
 * No dependencies, no browser. js/api.js is evaluated inside a Function with
 * hand-made `fetch`, `sessionStorage`, `AbortController` and `Date` stubs, and
 * a controllable fake clock, so a 75-second cold-start budget is exercised in
 * microseconds and the exact number of HTTP requests it produces is countable.
 *
 * What this pins down:
 *   - how much load the probe puts on a free-tier host that is asleep or
 *     starved of CPU (the reason the loop backs off at all);
 *   - that the ad-blocker/CORS/cold-start diagnosis is unchanged;
 *   - that a remembered working endpoint is probed first, so a repeat visitor
 *     whose /health is filtered does not pay for it again.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const API_JS = path.join(ROOT, 'js', 'api.js');
const HEALTH = JSON.stringify({ status: 'ok', version: '1.0', database: 'sqlite', parcels: 7 });

/* ------------------------------- fake browser ---------------------------- */

const RealDate = Date;

/**
 * @param {object} opts
 * @param {(path:string, n:number)=>object} opts.behaviour  what each request does
 * @param {Record<string,string>} [opts.session]  pre-seeded sessionStorage
 *
 * behaviour returns any of:
 *   { blocked: true }        reject instantly with a TypeError (extension kill)
 *   { ms, status?, body? }   answer after `ms` with an HTTP response
 *   { ms, abort: true }      hang for `ms`; the client's own timeout wins if shorter
 */
function makeEnv({ behaviour, session = {} }) {
  let now = 0, idc = 0, lastTimerMs = 0;
  const timers = new Map();
  const store = { ...session };
  const calls = [];

  const g = {
    Date: class extends RealDate { static now() { return now; } },
    setTimeout(fn, ms = 0) { const id = ++idc; lastTimerMs = ms; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeout(id) { timers.delete(id); },
    AbortController: class {
      constructor() { this.signal = { aborted: false }; }
      abort() { this.signal.aborted = true; }
    },
    URL, console,
    sessionStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
  };

  g.fetch = async (u) => {
    const p = String(u).replace(/^https?:\/\/[^/]*/, '') || '/';
    calls.push(p);
    const beh = behaviour(p, calls.length);
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

  g.location = { origin: 'https://demo.test', protocol: 'https:', hostname: 'demo.test' };
  g.window = {
    API_BASE_URL: 'https://api.demo.test',
    location: g.location,
    matchMedia: () => ({ matches: false }),
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
    return { res, requests: calls.length, paths: calls.slice(), elapsedMs: now,
             online: API.isOnline, failure: API.lastFailure, session: { ...store } };
  }

  return { API, calls, checkHealth };
}

/* --------------------------------- runner -------------------------------- */

let failures = 0, passed = 0;
function check(name, cond, detail = '') {
  if (cond) { passed++; console.log(`  ok    ${name}`); }
  else { failures++; console.log(`  FAIL  ${name}${detail ? `  -- ${detail}` : ''}`); }
}

const HANG = () => ({ ms: 60000, abort: true });          // container is asleep
const UP = () => ({ ms: 2 });                             // healthy
const BLOCKED_HEALTH = (p) => (p === '/health' ? { blocked: true } : { ms: 2 });
const STARTING = () => ({ ms: 5000, body: JSON.stringify({ detail: 'starting up' }) });
const SLOW_HEALTHY = () => ({ ms: 6000 });     // alive, but starved of CPU

console.log('\njs/api.js checkHealth()');

{
  const r = await makeEnv({ behaviour: UP }).checkHealth();
  check('warm host: reports live', r.online === true && r.res?.parcels === 7);
  check('warm host: one request only', r.requests === 1, `${r.requests} req`);
  check('warm host: remembers the path', r.session['ulpin.healthPath'] === '/health');
}

{
  const r = await makeEnv({ behaviour: BLOCKED_HEALTH }).checkHealth();
  check('blocked /health: falls through to /status', r.online === true);
  check('blocked /health: two requests', r.requests === 2, `${r.requests} req`);
  check('blocked /health: remembers /status', r.session['ulpin.healthPath'] === '/status');
}

{
  // The point of remembering it: no hang on a path this browser will never send.
  const r = await makeEnv({ behaviour: BLOCKED_HEALTH, session: { 'ulpin.healthPath': '/status' } }).checkHealth();
  check('returning visitor: one request', r.requests === 1, `${r.requests} req`);
  check('returning visitor: no time lost', r.elapsedMs < 1000, `${r.elapsedMs}ms`);
  check('returning visitor: still live', r.online === true);
}

{
  const r = await makeEnv({ behaviour: HANG }).checkHealth();
  check('asleep host: gives up, reports unreachable', r.online === false && r.failure === 'unreachable');
  check('asleep host: stays inside the budget', r.elapsedMs <= 130000, `${(r.elapsedMs / 1000).toFixed(0)}s`);
  check('asleep host: does not hammer', r.requests <= 12, `${r.requests} requests`);
}

{
  const r = await makeEnv({ behaviour: (p, n) => (n < 4 ? { ms: 20000, abort: true } : { ms: 2 }) }).checkHealth();
  check('waking host: recovers and reports live', r.online === true);
}

{
  const r = await makeEnv({ behaviour: () => ({ blocked: true }) }).checkHealth();
  check('everything blocked: diagnosed as blocked', r.online === false && r.failure === 'blocked');
  check('everything blocked: gives up fast', r.elapsedMs < 20000, `${(r.elapsedMs / 1000).toFixed(0)}s`);
}

{
  // A healthy host that is simply slow. This is the case that pins the warm
  // timeout: shrink it below the host's response time and every probe aborts,
  // the sweep burns four requests instead of one, and the pill takes ~20 s to
  // say "live" instead of ~6 s.
  const r = await makeEnv({ behaviour: SLOW_HEALTHY }).checkHealth();
  check('slow healthy host: reports live', r.online === true);
  check('slow healthy host: first probe succeeds', r.requests === 1, `${r.requests} req`);
  check('slow healthy host: no retry storm', r.elapsedMs < 10000, `${(r.elapsedMs / 1000).toFixed(1)}s`);
}

{
  // Alive but starved: every request costs the container 5 s of its 0.1 CPU.
  // "/" answers with a non-health body, which asHealth() treats as liveness.
  const r = await makeEnv({ behaviour: STARTING }).checkHealth();
  check('starved host: not diagnosed as blocked', r.failure === null);
  check('starved host: bounded request count', r.requests <= 8, `${r.requests} requests`);
}

console.log(`\n${passed} passed, ${failures} failed\n`);
process.exit(failures ? 1 : 0);
