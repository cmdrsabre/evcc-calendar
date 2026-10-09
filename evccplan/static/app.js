/* Charge planning: UI without a build step. All texts from data are set via textContent (no innerHTML with foreign data). */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const CAR_SVG = '<svg viewBox="0 0 320 120" aria-hidden="true"><path d="M14 86c0-9 4-15 12-18l34-10c10-14 22-24 40-30 20-6 52-6 76-2 28 5 44 18 60 34l44 8c14 3 26 8 32 16 5 7 4 12 4 16H14z"/><path class="glass" d="M82 56c8-9 16-16 30-19 20-4 40-3 56 0v21H82zM178 38c14 3 26 10 38 20h-38z"/><circle class="tyre" cx="84" cy="100" r="17"/><circle class="tyre" cx="244" cy="100" r="17"/><circle class="rim" cx="84" cy="100" r="7"/><circle class="rim" cx="244" cy="100" r="7"/></svg>';

  const S = { tab: 'plan', settings: null, status: null, pending: {}, cards: new Map(), sig: '', tz: 'Europe/Berlin', timer: null, fmt: null, targets: [], lang: null, dict: {}, fallback: {}, languages: [] };

  // ------------------------------------------------------------ Helpers
  function icon(name) {
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'ico');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVGNS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function el(tag, cls, ...kids) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    for (const k of kids) if (k != null) e.append(k);
    return e;
  }

  // ------------------------------------------------------------ Translations
  const LOCALES = { en: 'en-GB', de: 'de-DE' };
  const locale = () => LOCALES[S.lang] || S.lang || 'en-GB';

  function t(key, params) {
    let s = S.dict[key];
    if (s == null) s = S.fallback[key];
    if (s == null) s = key;
    if (!params) return s;
    return s.replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
  }

  async function fetchStrings(lang) {
    try {
      const r = await fetch('/locales/' + encodeURIComponent(lang) + '.json', { credentials: 'same-origin' });
      if (r.ok) return await r.json();
    } catch (e) { /* empty */ }
    return null;
  }

  // Loads the strings of a language (English as fallback) and re-renders everything.
  async function setLang(lang) {
    lang = lang || 'en';
    if (!S.fallback || !Object.keys(S.fallback).length) S.fallback = (await fetchStrings('en')) || {};
    S.dict = lang === 'en' ? S.fallback : ((await fetchStrings(lang)) || {});
    const changed = S.lang !== lang;
    S.lang = lang;
    document.documentElement.lang = lang;
    applyI18n();
    if (!changed) return;
    // Language switch: formatters, keyed cards and settings form must be rebuilt.
    S.cards.clear(); S.sig = ''; $('days').replaceChildren();
    S.inputs = null;
    if (S.fmt) S.fmt = makeFormatters(S.tz);
    if (S.status) render(S.status);
    if (!$('view-main').hidden) {
      if (S.tab === 'history') loadHistory();
      if (S.tab === 'settings') loadSettings();
    }
  }

  // Text with `code` segments: built from text nodes and <code> elements only.
  function setRich(node, text) {
    node.replaceChildren();
    text.split('`').forEach((part, i) => { if (part) node.append(i % 2 ? el('code', null, part) : part); });
  }

  function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach((n) => { n.textContent = t(n.dataset.i18n); });
    document.querySelectorAll('[data-i18n-rich]').forEach((n) => setRich(n, t(n.dataset.i18nRich)));
    document.querySelectorAll('[data-i18n-title]').forEach((n) => { n.title = t(n.dataset.i18nTitle); });
    document.querySelectorAll('[data-i18n-aria-label]').forEach((n) => { n.setAttribute('aria-label', t(n.dataset.i18nAriaLabel)); });
    document.querySelectorAll('[data-i18n-placeholder]').forEach((n) => { n.placeholder = t(n.dataset.i18nPlaceholder); });
    document.title = t('ui.title');
    if (S.status) { $('version').textContent = S.status.version ? t('ui.version', { v: S.status.version }) : ''; }
  }

  const num = (v) => { const x = v < 10 ? v.toFixed(1) : String(Math.round(v)); return S.lang === 'de' ? x.replace('.', ',') : x; };

  async function api(method, path, body) {
    const opt = { method, credentials: 'same-origin', headers: {} };
    if (method === 'POST') {
      opt.headers['Content-Type'] = 'application/json';
      opt.headers['X-CSRF'] = '1';
      opt.body = JSON.stringify(body || {});
    }
    let r;
    try { r = await fetch(path, opt); } catch (e) { return { ok: false, status: 0, data: { error: t('ui.err.unreachable') } }; }
    let data = null;
    try { data = await r.json(); } catch (e) { /* empty */ }
    return { ok: r.ok, status: r.status, data: data || {} };
  }

  let toastTimer = null;
  function toast(msg, isError) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' error' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 2500);
  }

  // Shows the built-in graphic first, then the first loadable image from the list (e.g. custom image before default image).
  function art(box, ...srcs) {
    box.innerHTML = CAR_SVG;
    const next = (i) => {
      if (i >= srcs.length) return;
      const img = new Image();
      img.alt = '';
      img.addEventListener('load', () => box.replaceChildren(img));
      img.addEventListener('error', () => next(i + 1));
      img.src = srcs[i];
    };
    next(0);
  }

  function makeFormatters(tz) {
    const f = (o) => new Intl.DateTimeFormat(locale(), Object.assign({ timeZone: tz }, o));
    return {
      time: f({ hour: '2-digit', minute: '2-digit' }),
      dayKey: new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }),
      dayLong: f({ weekday: 'long', day: 'numeric', month: 'long' }),
      short: f({ weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }),
    };
  }
  const d = (iso) => new Date(iso);

  // ------------------------------------------------------------ Login
  function showAuth(setupNeeded, minPw) {
    clearTimeout(S.timer);
    $('view-main').hidden = true;
    $('view-auth').hidden = false;
    $('progress').hidden = true;
    art($('auth-art'), 'img/car.webp');
    const form = $('auth-form');
    form.dataset.mode = setupNeeded ? 'setup' : 'login';
    $('auth-lead').textContent = t(setupNeeded ? 'ui.auth.lead_setup' : 'ui.auth.lead_login');
    $('auth-submit').textContent = t(setupNeeded ? 'ui.auth.submit_setup' : 'ui.auth.submit_login');
    $('pw2-wrap').hidden = !setupNeeded;
    $('pw2').required = setupNeeded;
    $('pw1').autocomplete = setupNeeded ? 'new-password' : 'current-password';
    $('pw1').minLength = setupNeeded ? (minPw || 8) : 1;
    $('pw1').value = ''; $('pw2').value = '';
    $('auth-error').hidden = true;
    $('pw1').focus();
  }

  async function submitAuth(ev) {
    ev.preventDefault();
    const setup = $('auth-form').dataset.mode === 'setup';
    const err = (m) => { $('auth-error').textContent = m; $('auth-error').hidden = false; };
    if (setup && $('pw1').value !== $('pw2').value) return err(t('ui.auth.mismatch'));
    $('auth-submit').disabled = true;
    const r = await api('POST', setup ? '/api/setup' : '/api/login', { password: $('pw1').value });
    $('auth-submit').disabled = false;
    if (!r.ok) return err(r.data.error || t('ui.err.generic'));
    startMain();
  }

  async function boot() {
    const r = await api('GET', '/api/me');
    await applyServerLang(r.data);
    if (r.ok && r.data.authed) return startMain();
    showAuth(!!r.data.setup_needed, r.data.min_password);
  }

  async function applyServerLang(data) {
    if (Array.isArray(data.languages)) S.languages = data.languages;
    if (data.language && data.language !== S.lang) await setLang(data.language);
    else if (!S.lang) await setLang('en');
  }

  // ------------------------------------------------------------ Main view
  function startMain() {
    $('view-auth').hidden = true;
    $('view-main').hidden = false;
    art($('car-art'), 'api/personal/car', 'img/car.webp');   // custom image (logged in only) before the default image
    refresh();
  }

  async function refresh() {
    clearTimeout(S.timer);
    const r = await api('GET', '/api/status');
    if (r.status === 401) { const me = await api('GET', '/api/me'); await applyServerLang(me.data); return showAuth(!!me.data.setup_needed, me.data.min_password); }
    if (r.ok) {
      if (!r.data.busy) S.pending = {};
      if (r.data.language && r.data.language !== S.lang) { S.status = null; await applyServerLang(r.data); }
      render(r.data);
    } else {
      toast(r.data.error || t('ui.err.status_load'), true);
    }
    if (S.tab === 'history' && r.ok && !r.data.busy) loadHistory();
    const busy = r.ok && r.data.busy;
    $('progress').hidden = !busy;
    $('btn-run').disabled = !!busy;
    S.timer = setTimeout(refresh, busy ? 1000 : (document.hidden ? 60000 : 15000));
  }

  const STATUS = {
    geplant: ['ui.status.geplant', 'plan', 'bolt', 'is-plan'],
    verkettet: ['ui.status.verkettet', '', 'car', ''],
    spaeter: ['ui.status.spaeter', '', 'clock', 'is-dim'],
    bahn: ['ui.status.bahn', '', 'train', 'is-bahn is-dim'],
    nah: ['ui.status.nah', '', 'walk', 'is-walk is-dim'],
    kein_ort: ['ui.status.kein_ort', '', 'pin', 'is-off is-dim'],
    adresse_unklar: ['ui.status.adresse_unklar', 'warn', 'alert', ''],
    zu_spaet: ['ui.status.zu_spaet', '', 'clock', 'is-dim'],
    unter_min: ['ui.status.unter_min', '', 'info', ''],
    ueberschneidung: ['ui.status.ueberschneidung', 'warn', 'alert', ''],
    manuell_aus: ['ui.status.manuell_aus', '', 'ban', 'is-off is-dim'],
    ziel_fehlt: ['ui.status.ziel_fehlt', 'bad', 'alert', ''],
    auto: ['ui.status.auto', '', 'car', ''],
  };

  function render(res) {
    S.status = res;
    S.tz = res.timezone || S.tz;
    S.fmt = makeFormatters(S.tz);
    S.targets = res.targets || [];
    $('version').textContent = res.version ? t('ui.version', { v: res.version }) : '';
    renderTop(res);
    renderVehicle(res);
    renderAlarm(res);
    renderItems(res);
    renderNotices(res);
  }

  function renderTop(res) {
    const chip = $('mode-chip');
    chip.textContent = t(res.config_dry_run ? 'ui.mode.dry' : 'ui.mode.live');
    chip.className = 'chip ' + (res.config_dry_run ? 'dry' : 'live');
    $('stamp').textContent = res.time ? t(res.stale_since ? 'ui.stamp_stale' : 'ui.stamp', { time: S.fmt.time.format(d(res.time)) }) : t('ui.stamp.none');
  }

  function renderVehicle(res) {
    const v = res.vehicle || {};
    $('v-title').textContent = v.title || t('ui.vehicle.title');
    const soc = typeof v.soc === 'number' ? v.soc : null;
    $('v-soc').textContent = soc == null ? '–' : String(Math.round(soc));
    $('v-fill').style.setProperty('--w', (soc || 0) + '%');
    $('v-bar').setAttribute('aria-label', soc == null ? t('ui.soc.unknown') : t('ui.soc.aria', { n: Math.round(soc) }));
    const dz = res.desired;
    $('v-flag').hidden = !dz;
    if (dz) { $('v-flag').style.setProperty('--x', dz.soc + '%'); $('v-flag-label').textContent = t('ui.pct', { n: dz.soc }); }

    const facts = $('v-facts');
    facts.replaceChildren();
    const chip = (ic, txt) => facts.append(el('li', 'chip', ic ? icon(ic) : null, txt));
    if (v.connected != null) chip('plug', t(v.connected ? 'ui.fact.connected' : 'ui.fact.disconnected'));
    if (v.car_limit != null) chip(null, t('ui.fact.car_limit', { n: v.car_limit }));
    if (v.min_soc != null) chip(null, t('ui.fact.min_soc', { n: v.min_soc }));
    if (v.mode) chip(null, t('ui.fact.evcc_mode', { mode: v.mode }));

    const box = $('plan-box');
    box.replaceChildren();
    if (dz) {
      box.append(el('p', 'plan-main', t('ui.plan.main', { soc: dz.soc, time: S.fmt.short.format(d(dz.time)) })));
      box.append(el('p', 'plan-sub', t('ui.plan.for', { title: dz.title })));
      const a = res.action || {};
      const lines = {
        set: t(res.dry_run ? 'ui.plan.set_dry' : (a.done ? 'ui.plan.set_done' : 'ui.plan.set_pending')),
        none: t('ui.plan.none_change'),
        manual: t('ui.plan.manual'),
        delete: t(res.dry_run ? 'ui.plan.delete_dry' : 'ui.plan.delete_done'),
      };
      box.append(el('p', 'plan-meta', lines[a.kind] || ''));
      if (dz.capped_from) box.append(el('p', 'plan-meta', t('ui.plan.capped', { n: dz.capped_from })));
    } else {
      box.append(el('p', 'plan-main', t('ui.plan.none')));
      if (res.skipped_reason) box.append(el('p', 'plan-meta', res.skipped_reason));
    }
  }

  function renderAlarm(res) {
    const box = $('alarm');
    const probs = res.problems || [];
    box.hidden = !probs.length;
    if (!probs.length) return;
    const ul = el('ul');
    probs.forEach((p) => ul.append(el('li', null, p)));
    box.replaceChildren(el('strong', null, t('ui.alarm.title')), ul);
    if (res.stale_since) box.append(el('p', null, t('ui.alarm.stale', { time: S.fmt.short.format(d(res.stale_since)) })));
  }

  function renderNotices(res) {
    const list = res.notices || [];
    $('notices').hidden = !list.length;
    $('notices-title').textContent = t('ui.notices.title', { n: list.length });
    const ul = $('notices-list');
    ul.replaceChildren();
    list.forEach((n) => ul.append(el('li', null, n.text + ' ', el('small', null, '(' + n.status + ')'))));
  }

  // ------------------------------------------------------------ Events
  function renderItems(res) {
    const items = res.items || [];
    const fresh = S.cards.size === 0;
    const seen = new Set();
    for (const it of items) {
      seen.add(it.key);
      let c = S.cards.get(it.key);
      if (!c) { c = buildCard(it.key); c.root.classList.add('fresh'); S.cards.set(it.key, c); }
      updateCard(c, it);
    }
    for (const [k, c] of S.cards) if (!seen.has(k)) { c.root.remove(); S.cards.delete(k); }

    const sig = items.map((i) => S.fmt.dayKey.format(d(i.start)) + i.key).join('|');
    if (sig !== S.sig) {
      S.sig = sig;
      const wrap = document.createDocumentFragment();
      const today = S.fmt.dayKey.format(d(res.now || res.time));
      const tomorrow = S.fmt.dayKey.format(new Date(d(res.now || res.time).getTime() + 86400000));
      let curDay = null, rail = null;
      for (const it of items) {
        const day = S.fmt.dayKey.format(d(it.start));
        if (day !== curDay) {
          curDay = day;
          const sec = el('section', 'day');
          const rel = day === today ? t('ui.day.today') : day === tomorrow ? t('ui.day.tomorrow') : '';
          sec.append(el('div', 'day-head', el('h2', null, S.fmt.dayLong.format(d(it.start))), rel ? el('span', null, rel) : null));
          rail = el('div', 'rail');
          sec.append(rail);
          wrap.append(sec);
        }
        rail.append(S.cards.get(it.key).root);
      }
      $('days').replaceChildren(wrap);
    }
    const empty = $('empty');
    empty.hidden = items.length > 0;
    if (!items.length) empty.textContent = t(res.error ? 'ui.empty.calendar_error' : 'ui.empty.none');
    if (fresh) setTimeout(() => S.cards.forEach((c) => c.root.classList.remove('fresh')), 1200);
  }

  function buildCard(key) {
    const r = {};
    r.root = el('article', 'card');
    r.when = el('span', 'when');
    r.title = el('h3');
    r.badge = el('span', 'badge');
    r.meta = el('div', 'meta');
    r.detail = el('p', 'detail');
    r.charge = el('div', 'charge');
    r.bar = el('div', 'bar');
    r.bar.setAttribute('role', 'img');
    r.flag = el('span', 'flag', (r.flagLabel = el('span', 'flag-label')));
    r.legend = el('div', 'legend');
    r.ready = el('span', 'ready');
    r.charge.append(r.bar, r.legend, r.ready);

    // Bedienung
    r.mode = el('div', 'seg-ctl');
    r.mode.setAttribute('role', 'radiogroup');
    r.mode.setAttribute('aria-label', t('ui.card.mode_aria'));
    r.modeBtns = {};
    [['', t('ui.card.mode_auto'), null], ['car', t('ui.card.mode_car'), 'car'], ['none', t('ui.card.mode_none'), 'ban']].forEach(([val, txt, ic]) => {
      const b = el('button', null, ic ? icon(ic) : null, txt);
      b.type = 'button';
      b.setAttribute('role', 'radio');
      b.addEventListener('click', () => change(key, { mode: val || null, target: val === 'none' ? null : cur(key).target }));
      r.modeBtns[val] = b;
      r.mode.append(b);
    });
    r.sel = el('select', 'sel');
    r.sel.id = 'sel-' + btoa(unescape(encodeURIComponent(key))).replace(/[^A-Za-z0-9]/g, '').slice(0, 40);
    r.optAuto = el('option');
    r.optAuto.value = '';
    r.sel.append(r.optAuto);
    S.targets.forEach((t) => { const o = el('option', null, t + ' %'); o.value = String(t); r.sel.append(o); });
    r.sel.addEventListener('change', () => {
      const t = r.sel.value === '' ? null : parseInt(r.sel.value, 10);
      const m = cur(key).mode;
      change(key, { mode: t != null && !m ? 'car' : m, target: t });
    });
    const lab = el('label', 'ctl', t('ui.card.target'), r.sel);
    lab.htmlFor = r.sel.id;
    r.reset = el('button', 'link', t('ui.card.reset'));
    r.reset.type = 'button';
    r.reset.addEventListener('click', () => change(key, { mode: null, target: null }));
    r.controls = el('div', 'controls', r.mode, lab, r.reset);

    r.root.append(el('div', 'head', r.when, r.title, r.badge), r.meta, r.detail, r.charge, r.controls);
    r.key = key;
    return r;
  }

  const items = () => (S.status && S.status.items) || [];
  function cur(key) {
    if (S.pending[key]) return S.pending[key];
    const it = items().find((i) => i.key === key) || {};
    return { mode: it.override_mode || null, target: it.override_target == null ? null : it.override_target };
  }

  function updateCard(c, it) {
    const st = STATUS[it.status] || [null, '', 'info', ''];
    const stLabel = st[0] ? t(st[0]) : it.status;
    const o = cur(it.key);
    c.root.className = 'card ' + st[3] + (c.root.classList.contains('fresh') ? ' fresh' : '');
    c.when.textContent = S.fmt.time.format(d(it.start)) + '–' + S.fmt.time.format(d(it.end));
    c.title.textContent = it.title || t('ui.card.untitled');
    c.badge.className = 'badge ' + st[1];
    c.badge.replaceChildren(icon(st[2]), it.unclear ? t('ui.card.unclear', { label: stLabel }) : stLabel);

    // Meta row
    c.meta.replaceChildren();
    const where = it.place || it.location;
    if (where) { const s = el('span', null, icon('pin'), where); s.title = it.location || ''; c.meta.append(s); }
    if (it.km != null) c.meta.append(el('span', null, icon('route'), t(it.estimated ? 'ui.card.km_est' : 'ui.card.km', { km: num(it.km), min: it.drive_min })));
    if (it.kwh100 != null) c.meta.append(el('span', null, icon('temp'), it.temp_c == null ? t('ui.card.temp_unknown_kwh', { kwh: num(it.kwh100) }) : t('ui.card.temp_kwh', { temp: num(it.temp_c), kwh: num(it.kwh100) })));
    c.meta.hidden = !c.meta.children.length;

    // Charge bar
    const T = it.target_chain != null ? it.target_chain : it.target_alone;
    c.charge.hidden = T == null;
    let short = false;
    if (T != null) {
      c.bar.replaceChildren();
      c.legend.replaceChildren();
      const seg = (cls, w) => { const s = el('span', 'seg ' + cls); s.style.setProperty('--w', w + '%'); c.bar.append(s); };
      if (it.manual || it.need_soc == null) {
        seg('manual', T);
        c.legend.append(el('span', null, t('ui.card.manual_target')));
      } else {
        let need = it.need_soc;
        const free = T - need;
        short = free < 0;
        if (short) need = T;
        seg('reserve', Math.max(0, T - need));
        seg('back', need / 2);
        seg('out', need / 2);
        const li = (cls, label, v) => { const s = el('span', cls, el('i'), label + ' '); s.append(el('b', null, num(v) + ' %')); c.legend.append(s); };
        li('l-out', t('ui.card.leg_out'), it.need_soc / 2);
        li('l-back', t('ui.card.leg_back'), it.need_soc / 2);
        li('l-reserve', t('ui.card.leg_reserve'), Math.max(0, free));
      }
      c.flag.style.setProperty('--x', T + '%');
      c.flagLabel.textContent = t('ui.pct', { n: T });
      c.bar.append(c.flag);
      c.bar.setAttribute('aria-label', t('ui.card.target_aria', { n: T }));
      c.ready.replaceChildren();
      if (it.ready_by) c.ready.append(icon('clock'), t('ui.card.ready_by', { time: S.fmt.short.format(d(it.ready_by)) }));
    }

    c.detail.className = 'detail' + (short || it.status === 'ziel_fehlt' ? ' alert' : '');
    c.detail.textContent = short ? t('ui.card.too_low') : (it.detail || '');
    c.detail.hidden = !c.detail.textContent;

    // Bedienung
    const modeKey = o.mode || '';
    for (const [k, b] of Object.entries(c.modeBtns)) b.setAttribute('aria-checked', String(k === modeKey));
    const auto = it.target_alone;
    c.optAuto.textContent = auto != null ? t('ui.card.auto_pct', { n: auto }) : t(it.has_location ? 'ui.card.auto' : 'ui.card.pick');
    if (document.activeElement !== c.sel) c.sel.value = o.target == null ? '' : String(o.target);
    c.reset.hidden = !(o.mode || o.target != null);
  }

  async function change(key, v) {
    S.pending[key] = v;
    const c = S.cards.get(key);
    const it = items().find((i) => i.key === key);
    if (c && it) updateCard(c, it);
    $('progress').hidden = false;
    const r = await api('POST', '/api/override', { key, mode: v.mode, target: v.target });
    if (!r.ok) { delete S.pending[key]; toast(r.data.error || t('ui.err.override_save'), true); }
    refresh();
  }

  // ------------------------------------------------------------ Tabs
  function showTab(name) {
    S.tab = name;
    document.querySelectorAll('#tabs button').forEach((b) => {
      if (b.dataset.tab === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    $('tab-plan').hidden = name !== 'plan';
    $('tab-history').hidden = name !== 'history';
    $('tab-settings').hidden = name !== 'settings';
    if (name === 'history') loadHistory();
    if (name === 'settings') loadSettings();
  }

  // ------------------------------------------------------------ History
  function runText(r) {
    if (!r.ok) return ['bad', t('ui.hist.error', { error: r.error || t('ui.hist.unknown') })];
    const plan = r.soc != null ? t('ui.hist.plan', { soc: r.soc, time: S.fmt.short.format(d(r.plan_time)), title: r.title }) : null;
    const a = r.action;
    if (a === 'set') return [r.done ? 'ok' : '', t(r.done ? 'ui.hist.set_done' : (r.dry_run ? 'ui.hist.set_dry' : 'ui.hist.set_failed'), { plan })];
    if (a === 'delete') return [r.done ? 'ok' : '', t(r.done ? 'ui.hist.delete_done' : 'ui.hist.delete_dry')];
    if (a === 'manual') return ['warn', t('ui.hist.manual')];
    return ['', plan ? t('ui.hist.no_change', { plan }) : t('ui.plan.none')];
  }

  async function loadHistory() {
    const r = await api('GET', '/api/history');
    if (r.status === 401) return boot();
    if (!r.ok) return toast(r.data.error || t('ui.err.history_load'), true);
    if (!S.fmt) S.fmt = makeFormatters(S.tz);
    const runs = r.data.runs || [];
    $('runs-empty').hidden = runs.length > 0;
    const ul = $('runs-list');
    ul.replaceChildren();
    runs.forEach((run) => {
      const [cls, txt] = runText(run);
      const li = el('li', 'run ' + cls);
      li.append(el('span', 'run-time', S.fmt.short.format(d(run.time))),
        el('span', 'chip ' + (run.dry_run ? 'dry' : 'live'), t(run.dry_run ? 'ui.hist.dry' : 'ui.hist.live')),
        el('span', 'run-text', txt));
      if (run.problems) li.append(el('span', 'run-note', t(run.problems > 1 ? 'ui.hist.problems_many' : 'ui.hist.problems_one', { n: run.problems })));
      ul.append(li);
    });
  }

  // ------------------------------------------------------------ Settings
  const GROUPS = [
    ['consumption', [['warm', 'kwh100'], ['cold', 'kwh100'], ['temp_threshold_c', 'c']]],
    ['charging', [['reserve_soc', 'pct'], ['unclear_cap_soc', 'pct'], ['charge_power_kw', 'kw'], ['charge_loss_margin', ''], ['vehicle_limit_min', 'pct']]],
    ['trips', [['min_car_km', 'km'], ['manual_drive_min', 'min'], ['time_buffer_min', 'min'], ['chain_window_h', 'h']]],
  ];

  // Language name in its own language (fallback: the code).
  function langName(code) {
    try { return new Intl.DisplayNames([code], { type: 'language' }).of(code) || code; } catch (e) { return code; }
  }

  function buildSettings(data) {
    const box = $('settings-groups');
    box.replaceChildren();
    S.inputs = {};
    const langs = Array.isArray(data.languages) && data.languages.length ? data.languages : S.languages;
    const gen = el('fieldset', 'group', el('legend', null, t('ui.set.group.general')));
    const lsel = el('select', 'sel');
    lsel.id = 'set-language';
    langs.forEach((code) => { const o = el('option', null, langName(code)); o.value = code; lsel.append(o); });
    S.langSel = lsel;
    gen.append(el('label', 'field row', el('span', null, t('ui.set.language')), el('span', 'inp', lsel)));
    box.append(gen);
    GROUPS.forEach(([group, fields]) => {
      const fs = el('fieldset', 'group', el('legend', null, t('ui.set.group.' + group)));
      fields.forEach(([key, unitKey]) => {
        const label = t('ui.set.' + key), unit = unitKey ? t('ui.unit.' + unitKey) : '';
        const lim = data.limits[key];
        const inp = el('input');
        inp.type = 'number';
        inp.id = 'set-' + key;
        inp.min = lim.min; inp.max = lim.max;
        inp.step = lim.int ? '1' : 'any';
        inp.required = true;
        S.inputs[key] = inp;
        const lab = el('label', 'field row', el('span', null, label), el('span', 'inp', inp, el('em', null, unit || '')));
        fs.append(lab);
      });
      box.append(fs);
    });
  }

  function fillSettings(data) {
    S.settings = data;
    if (S.langSel) S.langSel.value = data.values.language || S.lang;
    for (const [key, inp] of Object.entries(S.inputs)) {
      inp.value = String(data.values[key]);
      inp.parentElement.parentElement.classList.toggle('changed', data.changed.includes(key));
    }
    const ul = $('rules-list');
    ul.replaceChildren();
    data.values.rules.forEach((r) => addRuleRow(r.match, r.mode));
    $('settings-reset').hidden = !data.changed.length;
  }

  function addRuleRow(match, mode) {
    const m = el('input');
    m.type = 'text'; m.maxLength = 40; m.value = match || ''; m.placeholder = t('ui.rules.placeholder');
    m.setAttribute('aria-label', t('ui.rules.keyword'));
    const sel = el('select');
    [['auto', t('ui.rules.auto')], ['bahn', t('ui.rules.bahn')]].forEach(([v, t]) => { const o = el('option', null, t); o.value = v; sel.append(o); });
    sel.value = mode || 'auto';
    sel.setAttribute('aria-label', t('ui.rules.class'));
    const rm = el('button', 'link', t('ui.rules.remove'));
    rm.type = 'button';
    const li = el('li', 'rule', m, sel, rm);
    rm.addEventListener('click', () => li.remove());
    $('rules-list').append(li);
    return m;
  }

  function readRules() {
    return Array.from($('rules-list').children).map((li) => ({ match: li.querySelector('input').value.trim(), mode: li.querySelector('select').value }))
      .filter((r) => r.match);
  }

  async function loadSettings() {
    const r = await api('GET', '/api/settings');
    if (r.status === 401) return boot();
    if (!r.ok) return toast(r.data.error || t('ui.err.settings_load'), true);
    if (!S.inputs) buildSettings(r.data);
    fillSettings(r.data);
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const cur = S.settings.values, values = {};
    for (const [key, inp] of Object.entries(S.inputs)) {
      const v = Number(inp.value);
      if (inp.value === '' || Number.isNaN(v)) continue;
      if (v !== cur[key]) values[key] = v;
    }
    if (S.langSel && S.langSel.value && S.langSel.value !== cur.language) values.language = S.langSel.value;
    const rules = readRules();
    if (JSON.stringify(rules) !== JSON.stringify(cur.rules)) values.rules = rules;
    const err = $('settings-error');
    err.hidden = true;
    if (!Object.keys(values).length) return toast(t('ui.set.nothing'));
    $('settings-save').disabled = true;
    const r = await api('POST', '/api/settings', { values });
    $('settings-save').disabled = false;
    if (!r.ok) { err.textContent = r.data.error || t('ui.err.generic'); err.hidden = false; return; }
    await afterSettings(r.data);
    toast(t('ui.set.saved'));
    refresh();
  }

  // Takes over saved settings; switches the UI language if it changed.
  async function afterSettings(data) {
    S.settings = data;
    const lang = data.values && data.values.language;
    if (lang && lang !== S.lang) {
      if (Array.isArray(data.languages)) S.languages = data.languages;
      await setLang(lang);   // rebuilds the settings form (S.inputs = null) when the settings tab is visible
      if (S.inputs) return;
      buildSettings(data);
    }
    fillSettings(data);
  }

  async function resetSettings() {
    const r = await api('POST', '/api/settings', { reset: true });
    if (!r.ok) return toast(r.data.error || t('ui.err.generic'), true);
    await afterSettings(r.data);
    toast(t('ui.set.reset_done'));
    refresh();
  }

  // ------------------------------------------------------------ Header buttons
  $('auth-form').addEventListener('submit', submitAuth);
  $('tabs').addEventListener('click', (e) => { const b = e.target.closest('button[data-tab]'); if (b) showTab(b.dataset.tab); });
  $('settings-form').addEventListener('submit', saveSettings);
  $('settings-reset').addEventListener('click', resetSettings);
  $('rule-add').addEventListener('click', () => addRuleRow('', 'auto').focus());
  $('btn-logout').addEventListener('click', async () => { await api('POST', '/api/logout', {}); S.cards.clear(); S.sig = ''; $('days').replaceChildren(); boot(); });
  $('btn-run').addEventListener('click', async () => {
    $('btn-run').disabled = true;
    $('progress').hidden = false;
    const r = await api('POST', '/api/run', {});
    if (!r.ok) toast(r.data.error || t('ui.err.run_start'), true);
    refresh();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('view-main').hidden) refresh(); });

  boot();
})();
