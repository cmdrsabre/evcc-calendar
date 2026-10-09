/* Ladeplanung: Oberfläche ohne Build-Schritt. Alle Texte aus Daten werden per textContent gesetzt (kein innerHTML mit Fremddaten). */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const CAR_SVG = '<svg viewBox="0 0 320 120" aria-hidden="true"><path d="M14 86c0-9 4-15 12-18l34-10c10-14 22-24 40-30 20-6 52-6 76-2 28 5 44 18 60 34l44 8c14 3 26 8 32 16 5 7 4 12 4 16H14z"/><path class="glass" d="M82 56c8-9 16-16 30-19 20-4 40-3 56 0v21H82zM178 38c14 3 26 10 38 20h-38z"/><circle class="tyre" cx="84" cy="100" r="17"/><circle class="tyre" cx="244" cy="100" r="17"/><circle class="rim" cx="84" cy="100" r="7"/><circle class="rim" cx="244" cy="100" r="7"/></svg>';

  const S = { status: null, pending: {}, cards: new Map(), sig: '', tz: 'Europe/Berlin', timer: null, fmt: null, targets: [] };

  // ------------------------------------------------------------ Hilfen
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

  const num = (v) => (v < 10 ? v.toFixed(1) : String(Math.round(v))).replace('.', ',');

  async function api(method, path, body) {
    const opt = { method, credentials: 'same-origin', headers: {} };
    if (method === 'POST') {
      opt.headers['Content-Type'] = 'application/json';
      opt.headers['X-CSRF'] = '1';
      opt.body = JSON.stringify(body || {});
    }
    let r;
    try { r = await fetch(path, opt); } catch (e) { return { ok: false, status: 0, data: { error: 'Der Dienst ist nicht erreichbar.' } }; }
    let data = null;
    try { data = await r.json(); } catch (e) { /* leer */ }
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

  // Zeigt zuerst die eingebaute Grafik, dann das erste ladbare Bild aus der Liste (z. B. eigenes Bild vor Standardbild).
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
    const f = (o) => new Intl.DateTimeFormat('de-DE', Object.assign({ timeZone: tz }, o));
    return {
      time: f({ hour: '2-digit', minute: '2-digit' }),
      dayKey: new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }),
      dayLong: f({ weekday: 'long', day: 'numeric', month: 'long' }),
      short: f({ weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }),
    };
  }
  const d = (iso) => new Date(iso);

  // ------------------------------------------------------------ Anmeldung
  function showAuth(setupNeeded, minPw) {
    clearTimeout(S.timer);
    $('view-main').hidden = true;
    $('view-auth').hidden = false;
    $('progress').hidden = true;
    art($('auth-art'), 'img/car.webp');
    const form = $('auth-form');
    form.dataset.mode = setupNeeded ? 'setup' : 'login';
    $('auth-lead').textContent = setupNeeded
      ? 'Willkommen. Lege jetzt das Passwort für den Benutzer „admin“ fest. Es gilt für diese Oberfläche und lässt sich nur durch Löschen der Datenbank zurücksetzen.'
      : 'Melde dich an, um Termine und Ladeziele zu ändern.';
    $('auth-submit').textContent = setupNeeded ? 'Passwort festlegen' : 'Anmelden';
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
    if (setup && $('pw1').value !== $('pw2').value) return err('Die beiden Passwörter sind nicht gleich.');
    $('auth-submit').disabled = true;
    const r = await api('POST', setup ? '/api/setup' : '/api/login', { password: $('pw1').value });
    $('auth-submit').disabled = false;
    if (!r.ok) return err(r.data.error || 'Das hat nicht geklappt.');
    startMain();
  }

  async function boot() {
    const r = await api('GET', '/api/me');
    if (r.ok && r.data.authed) return startMain();
    showAuth(!!r.data.setup_needed, r.data.min_password);
  }

  // ------------------------------------------------------------ Hauptansicht
  function startMain() {
    $('view-auth').hidden = true;
    $('view-main').hidden = false;
    art($('car-art'), 'api/personal/car', 'img/car.webp');   // eigenes Bild (nur eingeloggt) vor dem Standardbild
    refresh();
  }

  async function refresh() {
    clearTimeout(S.timer);
    const r = await api('GET', '/api/status');
    if (r.status === 401) { const me = await api('GET', '/api/me'); return showAuth(!!me.data.setup_needed, me.data.min_password); }
    if (r.ok) {
      if (!r.data.busy) S.pending = {};
      render(r.data);
    } else {
      toast(r.data.error || 'Der Stand konnte nicht geladen werden.', true);
    }
    const busy = r.ok && r.data.busy;
    $('progress').hidden = !busy;
    $('btn-run').disabled = !!busy;
    S.timer = setTimeout(refresh, busy ? 1000 : (document.hidden ? 60000 : 15000));
  }

  const STATUS = {
    geplant: ['Wird geplant', 'plan', 'bolt', 'is-plan'],
    verkettet: ['Folgetermin', '', 'car', ''],
    spaeter: ['Später', '', 'clock', 'is-dim'],
    bahn: ['Bahn', '', 'train', 'is-bahn is-dim'],
    nah: ['Zu Fuß oder Rad', '', 'walk', 'is-walk is-dim'],
    kein_ort: ['Ohne Ort', '', 'pin', 'is-off is-dim'],
    adresse_unklar: ['Adresse unklar', 'warn', 'alert', ''],
    zu_spaet: ['Abfahrt vorbei', '', 'clock', 'is-dim'],
    unter_min: ['Unter evcc-Minimum', '', 'info', ''],
    ueberschneidung: ['Überschneidung', 'warn', 'alert', ''],
    manuell_aus: ['Kein Auto', '', 'ban', 'is-off is-dim'],
    ziel_fehlt: ['Ziel fehlt', 'bad', 'alert', ''],
    auto: ['Auto', '', 'car', ''],
  };

  function render(res) {
    S.status = res;
    S.tz = res.timezone || S.tz;
    S.fmt = makeFormatters(S.tz);
    S.targets = res.targets || [];
    $('version').textContent = res.version ? 'Version ' + res.version : '';
    renderTop(res);
    renderVehicle(res);
    renderAlarm(res);
    renderItems(res);
    renderNotices(res);
  }

  function renderTop(res) {
    const chip = $('mode-chip');
    chip.textContent = res.config_dry_run ? 'Dry-Run: es wird nichts geschrieben' : 'Scharf';
    chip.className = 'chip ' + (res.config_dry_run ? 'dry' : 'live');
    $('stamp').textContent = res.time ? 'Stand ' + S.fmt.time.format(d(res.time)) + ' Uhr' + (res.stale_since ? ' (ältere Daten)' : '') : 'Noch kein Lauf';
  }

  function renderVehicle(res) {
    const v = res.vehicle || {};
    $('v-title').textContent = v.title || 'Fahrzeug';
    const soc = typeof v.soc === 'number' ? v.soc : null;
    $('v-soc').textContent = soc == null ? '–' : String(Math.round(soc));
    $('v-fill').style.setProperty('--w', (soc || 0) + '%');
    $('v-bar').setAttribute('aria-label', soc == null ? 'Ladestand unbekannt' : 'Ladestand ' + Math.round(soc) + ' Prozent');
    const dz = res.desired;
    $('v-flag').hidden = !dz;
    if (dz) { $('v-flag').style.setProperty('--x', dz.soc + '%'); $('v-flag-label').textContent = dz.soc + ' %'; }

    const facts = $('v-facts');
    facts.replaceChildren();
    const chip = (ic, txt) => facts.append(el('li', 'chip', ic ? icon(ic) : null, txt));
    if (v.connected != null) chip('plug', v.connected ? 'Angesteckt' : 'Nicht angesteckt');
    if (v.car_limit != null) chip(null, 'Limit im Auto ' + v.car_limit + ' %');
    if (v.min_soc != null) chip(null, 'Minimum ' + v.min_soc + ' %');
    if (v.mode) chip(null, 'evcc: ' + v.mode);

    const box = $('plan-box');
    box.replaceChildren();
    if (dz) {
      box.append(el('p', 'plan-main', dz.soc + ' % bis ' + S.fmt.short.format(d(dz.time)) + ' Uhr'));
      box.append(el('p', 'plan-sub', 'für „' + dz.title + '“'));
      const a = res.action || {};
      const lines = {
        set: res.dry_run ? 'Würde in evcc gesetzt (Dry-Run).' : (a.done ? 'In evcc gesetzt.' : 'Wird in evcc gesetzt.'),
        none: 'Entspricht dem Plan in evcc.',
        manual: 'In evcc steht ein manueller Plan. Der bleibt unberührt.',
        delete: res.dry_run ? 'Würde den alten Plan entfernen (Dry-Run).' : 'Alter Plan entfernt.',
      };
      box.append(el('p', 'plan-meta', lines[a.kind] || ''));
      if (dz.capped_from) box.append(el('p', 'plan-meta', 'Gedeckelt von ' + dz.capped_from + ' %, weil die Einstufung unklar ist.'));
    } else {
      box.append(el('p', 'plan-main', 'Kein Ladeplan nötig'));
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
    box.replaceChildren(el('strong', null, 'Achtung'), ul);
    if (res.stale_since) box.append(el('p', null, 'Angezeigte Daten stammen von ' + S.fmt.short.format(d(res.stale_since)) + ' Uhr.'));
  }

  function renderNotices(res) {
    const list = res.notices || [];
    $('notices').hidden = !list.length;
    $('notices-title').textContent = 'Hinweise (' + list.length + ')';
    const ul = $('notices-list');
    ul.replaceChildren();
    list.forEach((n) => ul.append(el('li', null, n.text + ' ', el('small', null, '(' + n.status + ')'))));
  }

  // ------------------------------------------------------------ Termine
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
          const rel = day === today ? 'Heute' : day === tomorrow ? 'Morgen' : '';
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
    if (!items.length) empty.textContent = res.error ? 'Der Kalender konnte nicht gelesen werden.' : 'Im Kalender steht in den nächsten Tagen kein Termin mit Uhrzeit.';
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
    r.mode.setAttribute('aria-label', 'Auto für diesen Termin');
    r.modeBtns = {};
    [['', 'Automatisch', null], ['car', 'Auto', 'car'], ['none', 'Kein Auto', 'ban']].forEach(([val, txt, ic]) => {
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
    const lab = el('label', 'ctl', 'Ladeziel', r.sel);
    lab.htmlFor = r.sel.id;
    r.reset = el('button', 'link', 'Zurücksetzen');
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
    const st = STATUS[it.status] || [it.status, '', 'info', ''];
    const o = cur(it.key);
    c.root.className = 'card ' + st[3] + (c.root.classList.contains('fresh') ? ' fresh' : '');
    c.when.textContent = S.fmt.time.format(d(it.start)) + '–' + S.fmt.time.format(d(it.end));
    c.title.textContent = it.title || '(ohne Titel)';
    c.badge.className = 'badge ' + st[1];
    c.badge.replaceChildren(icon(st[2]), st[0] + (it.unclear ? ' (unklar)' : ''));

    // Meta-Zeile
    c.meta.replaceChildren();
    const where = it.place || it.location;
    if (where) { const s = el('span', null, icon('pin'), where); s.title = it.location || ''; c.meta.append(s); }
    if (it.km != null) c.meta.append(el('span', null, icon('route'), num(it.km) + ' km einfach, ' + it.drive_min + ' min' + (it.estimated ? ' (Luftlinie)' : '')));
    if (it.kwh100 != null) c.meta.append(el('span', null, icon('temp'), (it.temp_c == null ? 'Temperatur unbekannt' : num(it.temp_c) + ' °C') + ', ' + num(it.kwh100) + ' kWh/100 km'));
    c.meta.hidden = !c.meta.children.length;

    // Ladebalken
    const T = it.target_chain != null ? it.target_chain : it.target_alone;
    c.charge.hidden = T == null;
    let short = false;
    if (T != null) {
      c.bar.replaceChildren();
      c.legend.replaceChildren();
      const seg = (cls, w) => { const s = el('span', 'seg ' + cls); s.style.setProperty('--w', w + '%'); c.bar.append(s); };
      if (it.manual || it.need_soc == null) {
        seg('manual', T);
        c.legend.append(el('span', null, 'Ziel von Hand gewählt, Strecke unbekannt'));
      } else {
        let need = it.need_soc;
        const free = T - need;
        short = free < 0;
        if (short) need = T;
        seg('reserve', Math.max(0, T - need));
        seg('back', need / 2);
        seg('out', need / 2);
        const li = (cls, label, v) => { const s = el('span', cls, el('i'), label + ' '); s.append(el('b', null, num(v) + ' %')); c.legend.append(s); };
        li('l-out', 'Hinfahrt', it.need_soc / 2);
        li('l-back', 'Rückfahrt', it.need_soc / 2);
        li('l-reserve', 'Reserve', Math.max(0, free));
      }
      c.flag.style.setProperty('--x', T + '%');
      c.flagLabel.textContent = T + ' %';
      c.bar.append(c.flag);
      c.bar.setAttribute('aria-label', 'Ladeziel ' + T + ' Prozent');
      c.ready.replaceChildren();
      if (it.ready_by) c.ready.append(icon('clock'), 'Geladen bis ' + S.fmt.short.format(d(it.ready_by)) + ' Uhr');
    }

    c.detail.className = 'detail' + (short || it.status === 'ziel_fehlt' ? ' alert' : '');
    c.detail.textContent = short ? 'Dieses Ziel reicht nicht für Hin- und Rückfahrt.' : (it.detail || '');
    c.detail.hidden = !c.detail.textContent;

    // Bedienung
    const modeKey = o.mode || '';
    for (const [k, b] of Object.entries(c.modeBtns)) b.setAttribute('aria-checked', String(k === modeKey));
    const auto = it.target_alone;
    c.optAuto.textContent = auto != null ? 'Automatisch (' + auto + ' %)' : (it.has_location ? 'Automatisch' : 'Ziel wählen');
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
    if (!r.ok) { delete S.pending[key]; toast(r.data.error || 'Das konnte nicht gespeichert werden.', true); }
    refresh();
  }

  // ------------------------------------------------------------ Kopfzeilen-Knöpfe
  $('auth-form').addEventListener('submit', submitAuth);
  $('btn-logout').addEventListener('click', async () => { await api('POST', '/api/logout', {}); S.cards.clear(); S.sig = ''; $('days').replaceChildren(); boot(); });
  $('btn-run').addEventListener('click', async () => {
    $('btn-run').disabled = true;
    $('progress').hidden = false;
    const r = await api('POST', '/api/run', {});
    if (!r.ok) toast(r.data.error || 'Der Lauf konnte nicht gestartet werden.', true);
    refresh();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('view-main').hidden) refresh(); });

  boot();
})();
