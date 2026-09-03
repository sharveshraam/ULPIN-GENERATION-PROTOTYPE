/**
 * UI primitives: toasts, loading overlay, progress, modals, scroll reveal.
 * Deliberately dependency-free.
 */
const UI = (() => {
  /* ----------------------------- Toasts ----------------------------- */
  const ICONS = {
    success: '<path d="m5 13 4 4L19 7"/>',
    error: '<path d="M12 8v5m0 4h.01"/><circle cx="12" cy="12" r="9"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>',
    warn: '<path d="M12 9v4m0 4h.01"/><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6A2 2 0 0 0 22 18L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  };
  const TONE = {
    success: 'border-emerald-400/40 text-emerald-300',
    error: 'border-rose-400/40 text-rose-300',
    info: 'border-sky-400/40 text-sky-300',
    warn: 'border-amber-400/40 text-amber-300',
  };

  function toast(message, type = 'info', ttl = 4500) {
    const host = document.getElementById('toasts');
    if (!host) return;
    const el = document.createElement('div');
    el.className = `toast glass rounded-xl px-4 py-3 text-sm flex items-start gap-3 ${TONE[type] || TONE.info}`;
    el.innerHTML =
      `<svg class="w-4 h-4 mt-0.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">${ICONS[type] || ICONS.info}</svg>
       <span class="text-slate-100 leading-snug flex-1">${message}</span>`;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .3s, transform .3s';
      el.style.opacity = '0';
      el.style.transform = 'translateY(-10px)';
      setTimeout(() => el.remove(), 320);
    }, ttl);
  }

  /* --------------------------- Loading UI --------------------------- */
  function showLoader(title, subtitle, pct = null) {
    const box = document.getElementById('loader');
    if (!box) return;
    const t = document.getElementById('loaderText');
    const s = document.getElementById('loaderSub');
    const bar = document.getElementById('loaderBar');
    const wrap = document.getElementById('loaderBarWrap');
    if (t) t.textContent = title;
    if (s && subtitle !== undefined) s.textContent = subtitle;
    if (pct === null) {
      wrap?.classList.add('hidden');
    } else {
      wrap?.classList.remove('hidden');
      if (bar) bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    }
    box.classList.remove('hidden');
  }

  const hideLoader = () => document.getElementById('loader')?.classList.add('hidden');

  /** Skeleton rows for list placeholders. */
  const skeleton = (rows = 3, h = 'h-8') =>
    Array.from({ length: rows }, () => `<div class="shimmer ${h} rounded-lg"></div>`).join('');

  /* ----------------------------- Modal ------------------------------ */
  function openModal(id) {
    const m = document.getElementById(id);
    if (!m) return;
    m.classList.remove('hidden');
    requestAnimationFrame(() => m.classList.add('modal-open'));
    document.body.style.overflow = 'hidden';
  }

  function closeModal(id) {
    const m = document.getElementById(id);
    if (!m) return;
    m.classList.remove('modal-open');
    setTimeout(() => m.classList.add('hidden'), 220);
    document.body.style.overflow = '';
  }

  /* ------------------------- Scroll reveal -------------------------- */
  function initScrollReveal() {
    const els = document.querySelectorAll('[data-reveal]');
    if (!('IntersectionObserver' in window)) {
      els.forEach(e => e.classList.add('revealed'));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const delay = parseInt(entry.target.dataset.revealDelay || '0', 10);
          setTimeout(() => entry.target.classList.add('revealed'), delay);
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -60px 0px' });
    els.forEach(e => io.observe(e));
  }

  /* --------------------------- Utilities ---------------------------- */
  function copy(text, label = 'Copied to clipboard') {
    navigator.clipboard?.writeText(text)
      .then(() => toast(label, 'success', 2000))
      .catch(() => toast('Clipboard unavailable in this browser.', 'error'));
  }

  function download(filename, content, mime = 'application/json') {
    const blob = new Blob([content], { type: mime });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast(`Downloaded ${filename}`, 'success', 2500);
  }

  function toCSV(rows) {
    if (!rows.length) return '';
    const cols = Object.keys(rows[0]);
    const esc = (v) => {
      const s = v == null ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [cols.join(','), ...rows.map(r => cols.map(c => esc(r[c])).join(','))].join('\n');
  }

  const fmtArea = (m2) =>
    m2 >= 1e6 ? `${(m2 / 1e6).toFixed(2)} km²`
      : m2 >= 10000 ? `${(m2 / 10000).toFixed(2)} ha`
        : `${Math.round(m2).toLocaleString()} m²`;

  /** Animated number count-up. */
  function countUp(el, target, { suffix = '', decimals = 0, duration = 700 } = {}) {
    if (!el) return;
    const t0 = performance.now();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / duration);
      const eased = 1 - Math.pow(1 - k, 3);
      el.textContent = (target * eased).toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ',') + suffix;
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  return {
    toast, showLoader, hideLoader, skeleton,
    openModal, closeModal, initScrollReveal,
    copy, download, toCSV, fmtArea, countUp,
  };
})();

window.UI = UI;
