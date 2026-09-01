/**
 * "Connect API" dialog.
 *
 * Lets a visitor point the static frontend at a deployed backend without
 * editing any files: paste the Render URL, it is validated against /health,
 * then saved to localStorage so it survives reloads.
 *
 * The dialog is injected at runtime so index.html and map.html both get it
 * from a single source. Clicking the API status pill opens it.
 */
const ApiConnect = (() => {
  const MODAL_ID = 'modalApiConnect';
  let built = false;

  function build() {
    if (built) return;
    built = true;

    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <div id="${MODAL_ID}" class="modal hidden fixed inset-0 z-[80] bg-slate-950/80 backdrop-blur-sm p-4 flex items-start justify-center">
        <div class="modal-card glass rounded-2xl w-full max-w-lg mt-[10vh] overflow-hidden">
          <div class="px-5 py-3.5 border-b border-white/10 flex items-center justify-between gap-4">
            <div class="min-w-0">
              <h3 class="font-bold">Connect to backend API</h3>
              <p class="text-[11px] text-slate-500">Point this page at your deployed FastAPI service.</p>
            </div>
            <button type="button" data-ac-close
              class="text-slate-400 hover:text-white text-xl leading-none px-2">&times;</button>
          </div>

          <div class="p-5 space-y-4">
            <div>
              <label for="acUrl" class="block text-[11px] uppercase tracking-wider text-slate-400 mb-1.5">
                API base URL
              </label>
              <input id="acUrl" type="url" spellcheck="false" autocomplete="off"
                placeholder="https://your-service.onrender.com"
                class="w-full bg-slate-900/70 border border-white/10 rounded-lg px-3 py-2 text-sm
                       focus:outline-none focus:ring-2 focus:ring-indigo-500/60" />
              <p class="text-[11px] text-slate-500 mt-1.5">
                No trailing slash. Must be <strong>https</strong> if this page is served over https.
              </p>
            </div>

            <div id="acStatus" class="hidden text-xs rounded-lg px-3 py-2 border"></div>

            <div class="text-[11px] text-slate-500 leading-relaxed">
              Currently using:
              <code id="acCurrent" class="text-slate-300 break-all">—</code>
            </div>
          </div>

          <div class="px-5 py-3.5 border-t border-white/10 flex items-center justify-between gap-3">
            <button type="button" data-ac-reset
              class="text-[11px] text-slate-400 hover:text-white underline underline-offset-2">
              Reset to default
            </button>
            <div class="flex items-center gap-2">
              <button type="button" data-ac-close
                class="px-3 py-1.5 rounded-lg text-xs border border-white/10 hover:bg-white/5">Cancel</button>
              <button type="button" data-ac-save
                class="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500">
                Test &amp; save
              </button>
            </div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(wrap.firstElementChild);

    const modal = document.getElementById(MODAL_ID);
    modal.querySelectorAll('[data-ac-close]').forEach(b =>
      b.addEventListener('click', close));
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    modal.querySelector('[data-ac-save]').addEventListener('click', save);
    modal.querySelector('[data-ac-reset]').addEventListener('click', reset);
    modal.querySelector('#acUrl').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') save();
    });
  }

  function setStatus(msg, tone = 'info') {
    const el = document.getElementById('acStatus');
    const tones = {
      info: 'border-sky-500/30 bg-sky-500/10 text-sky-200',
      ok: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200',
      err: 'border-rose-500/30 bg-rose-500/10 text-rose-200',
    };
    el.className = `text-xs rounded-lg px-3 py-2 border ${tones[tone] || tones.info}`;
    el.innerHTML = msg;
    el.classList.remove('hidden');
  }

  function open() {
    build();
    const input = document.getElementById('acUrl');
    input.value = API.base || '';
    document.getElementById('acCurrent').textContent =
      API.base || '(same origin — no backend configured)';
    document.getElementById('acStatus').classList.add('hidden');
    UI.openModal(MODAL_ID);
    setTimeout(() => input.focus(), 60);
  }

  function close() { UI.closeModal(MODAL_ID); }

  async function save() {
    const raw = document.getElementById('acUrl').value.trim().replace(/\/$/, '');
    if (!raw) { setStatus('Enter a URL, or use “Reset to default”.', 'err'); return; }
    if (!/^https?:\/\//i.test(raw)) {
      setStatus('URL must start with http:// or https://', 'err'); return;
    }
    // A https page cannot call a http API — the browser blocks it as mixed
    // content, and the failure is silent in some browsers. Catch it early.
    if (location.protocol === 'https:' && raw.startsWith('http://')) {
      setStatus('This page is served over <strong>https</strong>, so it cannot call an ' +
                '<strong>http</strong> API — the browser blocks mixed content. Use the https URL.', 'err');
      return;
    }

    setStatus('Testing <code>' + raw + '/health</code> …');
    try {
      const health = await API.testBase(raw);
      API.base = raw;
      setStatus(`Connected. Backend reports <strong>${health.parcels ?? 0}</strong> stored parcels. Reloading…`, 'ok');
      setTimeout(() => location.reload(), 900);
    } catch (err) {
      setStatus(
        `Could not reach <code>${raw}/health</code>.<br>` +
        `<span class="text-slate-400">${err.message || err}</span><br><br>` +
        'Common causes: the service is still waking from sleep (free tier can take ~50s — try again), ' +
        'the URL is wrong, or CORS is not allowing this origin ' +
        `(<code>${location.origin}</code>).`, 'err');
    }
  }

  function reset() {
    const base = API.clearBase();
    setStatus(`Cleared. Now using: <code>${base || '(same origin)'}</code>. Reloading…`, 'ok');
    setTimeout(() => location.reload(), 900);
  }

  /** Make the status pill clickable on whichever page included this file. */
  function attach() {
    const pill = document.getElementById('apiPill');
    if (!pill) return;
    pill.style.cursor = 'pointer';
    pill.setAttribute('role', 'button');
    pill.setAttribute('tabindex', '0');
    pill.addEventListener('click', open);
    pill.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  }

  return { open, close, attach };
})();

window.ApiConnect = ApiConnect;
