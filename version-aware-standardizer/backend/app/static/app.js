/* Terminology Console -- a small hash-routed app over the project's own REST API.
   Vanilla JS on purpose: no build step, no CDN, works with the network cable out.

   The one idea the whole UI is built around: a verdict is never shown as a bare
   code. It always carries the status, the decision, the release it was judged
   against, and a sentence saying what that means -- because a decision without
   its release is exactly the ambiguity this project exists to remove. */

'use strict';

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $('#view');

function h(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
const n = v => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString('en-US');
const pct = v => (v === null || v === undefined) ? '—' : (v * 100).toFixed(2) + '%';
const dash = v => (v === null || v === undefined || v === '') ? '<span class="faint">—</span>' : h(v);

function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return h(iso);
  return d.toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = null;
  const text = await res.text();
  if (text) { try { body = JSON.parse(text); } catch { body = text; } }
  if (!res.ok) {
    const detail = (body && body.detail) ? body.detail : (body || res.statusText);
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return body;
}

function toast(msg, kind = '') {
  const host = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 9000 : 4500);
}

function modal(title, inner) {
  const host = $('#modalHost');
  host.innerHTML = `<div class="modal-host" id="mh"><div class="modal">
      <button class="quiet sm close" onclick="closeModal()">বন্ধ</button>
      <h3>${title}</h3>${inner}</div></div>`;
  $('#mh').addEventListener('click', e => { if (e.target.id === 'mh') closeModal(); });
}
function closeModal() { $('#modalHost').innerHTML = ''; }
window.closeModal = closeModal;

function loading(msg = 'লোড হচ্ছে…') {
  view().innerHTML = `<div class="empty"><span class="spin"></span><p>${h(msg)}</p></div>`;
}

function failed(err, what) {
  view().innerHTML = `<div class="page-head"><h1>কিছু একটা ভুল হলো</h1></div>
    <div class="note bad"><p><b>${h(what)}</b></p><p class="mono">${h(err.message)}</p>
    ${err.status === 503 ? '<p>সাধারণত এর মানে দরকারি একটা release এখনো import করা হয়নি।</p>' : ''}</div>`;
}

// ------------------------------------------------------- the vocabulary
// Every machine word the API can return, with the plain sentence that explains
// it. Kept in one place so the UI can never invent a meaning of its own.
const DECISION = {
  KEEP: {
    tone: 'keep', bn: 'ঠিক আছে', en: 'KEEP',
    say: 'কোডটি বর্তমান release-এ এখনো বৈধ। কিছু করার নেই।',
  },
  KEEP_WITH_WARNING: {
    tone: 'warning', bn: 'রাখুন, তবে সতর্কতা', en: 'KEEP_WITH_WARNING',
    say: 'কোডটি TRIAL — পরীক্ষামূলক। ব্যবহার করা যায়, কিন্তু ভবিষ্যতে বদলাতে পারে।',
  },
  SUGGEST_REPLACEMENT: {
    tone: 'suggest', bn: 'একটি official বিকল্প আছে', en: 'SUGGEST_REPLACEMENT',
    say: 'terminology নিজেই ঠিক একটি উত্তরসূরি ঘোষণা করেছে। যন্ত্র সেটি প্রস্তাব করছে — '
       + 'কিন্তু নিজে থেকে বদলাবে না; একজন মানুষকে অনুমোদন দিতে হবে।',
  },
  MANUAL_REVIEW: {
    tone: 'review', bn: 'মানুষ দেখুন', en: 'MANUAL_REVIEW',
    say: 'যন্ত্র ইচ্ছাকৃতভাবে সিদ্ধান্ত নেয়নি। অনুমান করার চেয়ে হাত তুলে দেওয়া নিরাপদ।',
  },
  UNKNOWN_CODE: {
    tone: 'unknown', bn: 'কোডটি চেনা গেল না', en: 'UNKNOWN_CODE',
    say: 'বর্তমান release-এ এই কোডটি নেই। টাইপো, অন্য terminology, বা কখনো বৈধ ছিল না।',
  },
};

const STATUS = {
  CURRENT_VALID:  { tone: 'keep',    bn: 'বৈধ' },
  CURRENT_TRIAL:  { tone: 'warning', bn: 'পরীক্ষামূলক (TRIAL)' },
  DISCOURAGED:    { tone: 'warning', bn: 'নিরুৎসাহিত' },
  DEPRECATED:     { tone: 'unknown', bn: 'পরিত্যক্ত' },
  INACTIVE:       { tone: 'unknown', bn: 'নিষ্ক্রিয়' },
  UNKNOWN:        { tone: 'unknown', bn: 'অজানা' },
};

const REASON = {
  STATUS_ACTIVE: 'বর্তমান release-এ ACTIVE।',
  STATUS_TRIAL: 'বর্তমান release-এ TRIAL।',
  SINGLE_OFFICIAL_REPLACEMENT: 'ঠিক একটি official বিকল্প ঘোষিত আছে।',
  MULTIPLE_REPLACEMENTS: 'একাধিক official বিকল্প আছে — কোনটি ঠিক তা স্থানীয় প্রেক্ষাপটের উপর নির্ভর করে।',
  NO_OFFICIAL_REPLACEMENT: 'কোনো official বিকল্প ঘোষণা করা হয়নি।',
  NO_HISTORICAL_ASSOCIATION: 'নিষ্ক্রিয়, কিন্তু কোনো সক্রিয় historical association নেই।',
  AMBIGUOUS_ASSOCIATION_TYPE: 'association-এর ধরনটি নিরাপদ প্রতিস্থাপনের জন্য যথেষ্ট নিশ্চিত নয়।',
  REPLACEMENT_TARGET_NOT_CURRENT: 'প্রস্তাবিত লক্ষ্যটি নিজেই আর বৈধ নয়।',
  REPLACEMENT_CHAIN_CYCLE: 'বিকল্পের শৃঙ্খল চক্রে ঘুরছে।',
  REPLACEMENT_CHAIN_TOO_DEEP: 'শৃঙ্খল নিরাপদ সীমার চেয়ে গভীর।',
  CODE_NOT_IN_CURRENT_RELEASE: 'বর্তমান release-এ কোডটি নেই।',
  NO_CURRENT_RELEASE: 'এই terminology-র কোনো release import করা হয়নি।',
  MOVED_TO_OTHER_NAMESPACE: 'অন্য namespace-এ সরানো হয়েছে — এটি ক্লিনিক্যাল বিকল্প নয়।',
};

const dec = d => DECISION[d] || { tone: 'neutral', bn: d, en: d, say: '' };
const stat = s => STATUS[s] || { tone: 'neutral', bn: s };
const pill = (d) => `<span class="pill ${dec(d).tone}"><span class="dot"></span>${h(dec(d).bn)}</span>`;
const statPill = (s) => `<span class="pill ${stat(s).tone}">${h(stat(s).bn)}</span>`;

// ------------------------------------------------------------------ state
const state = { health: null, releases: null, runs: null };

// ------------------------------------------------------------------ router
const ROUTES = {};
function go() {
  const raw = (location.hash || '#/dashboard').replace(/^#\/?/, '');
  const [name, ...rest] = raw.split('/');
  const route = ROUTES[name] ? name : 'dashboard';
  document.querySelectorAll('#nav a').forEach(a =>
    a.classList.toggle('on', a.dataset.r === route));
  window.scrollTo(0, 0);
  ROUTES[route](rest);
}
window.addEventListener('hashchange', go);

// ================================================================= DASHBOARD
ROUTES.dashboard = async () => {
  loading();
  try {
    const [health, releases, runs] = await Promise.all([
      api('/health'), api('/api/v1/releases'), api('/api/v1/audits?limit=5'),
    ]);
    state.health = health; state.releases = releases; state.runs = runs;

    const cur = health.releases || {};
    const run = runs[0];
    const s = run && run.summary_json ? run.summary_json : null;

    let head = `<div class="page-head">
      <h1>ড্যাশবোর্ড</h1>
      <p class="lede">এই মুহূর্তে সিস্টেম কোন terminology সংস্করণে কথা বলছে, আর
      সর্বশেষ নিরীক্ষা কী বলেছে।</p></div>`;

    // -- releases in force
    let rel = '<div class="grid c2">';
    for (const sys of ['LOINC', 'SNOMED_CT']) {
      const r = cur[sys];
      rel += `<div class="card mb0">
        <h2>${sys === 'LOINC' ? 'LOINC' : 'SNOMED CT'}</h2>
        ${r ? `<div class="stat" style="border:0;padding:0">
            <div class="n">${h(r.version)}</div>
            <div class="k">${r.effective_date ? 'কার্যকর ' + h(r.effective_date) : 'কার্যকর তারিখ দেওয়া নেই'}</div>
          </div>
          <dl class="kv mt">
            <dt>ফাইল</dt><dd class="mono small">${h(r.source_filename)}</dd>
            <dt>import</dt><dd>${when(r.imported_at)}</dd>
            <dt>অবস্থা</dt><dd>${h(r.import_status)}</dd>
            <dt>SHA-256</dt><dd class="mono small faint">${h(r.sha256.slice(0, 24))}…</dd>
          </dl>`
        : `<div class="note warn mb0"><p>এখনো কোনো release import করা হয়নি।</p></div>`}
      </div>`;
    }
    rel += '</div>';

    // -- latest audit
    let audit = '';
    if (s) {
      const bars = [
        ['keep', s.decisions?.KEEP || 0, 'ঠিক আছে'],
        ['warning', s.decisions?.KEEP_WITH_WARNING || 0, 'সতর্কতা'],
        ['suggest', s.decisions?.SUGGEST_REPLACEMENT || 0, 'বিকল্প আছে'],
        ['review', s.decisions?.MANUAL_REVIEW || 0, 'মানুষ দেখুন'],
        ['unknown', s.decisions?.UNKNOWN_CODE || 0, 'অজানা'],
      ].filter(b => b[1] > 0);
      const total = s.total_mappings || 1;
      const stale = (s.discouraged || 0) + (s.deprecated || 0) + (s.inactive_snomed || 0);

      audit = `<div class="card">
        <h2>সর্বশেষ নিরীক্ষা <span class="chip">run #${run.id}</span></h2>
        <p class="hint">${when(run.started_at)} · LOINC ${h(run.loinc_version || '—')}
          ${run.snomed_version ? ' · SNOMED ' + h(run.snomed_version) : ''}
          ${run.scope_json?.source_dataset ? ' · dataset ' + h(run.scope_json.source_dataset) : ''}</p>

        <div class="grid c4">
          <div class="stat plain"><div class="n plain">${n(s.total_mappings)}</div><div class="k">নিরীক্ষিত ম্যাপিং</div></div>
          <div class="stat ok"><div class="n">${n(s.valid)}</div><div class="k">এখনো বৈধ</div></div>
          <div class="stat bad"><div class="n">${n(stale)}</div>
            <div class="k">বাসি হয়ে গেছে</div>
            <div class="sub">${pct(stale / total)}</div></div>
          <div class="stat warn"><div class="n">${n(s.manual_review_required)}</div>
            <div class="k">মানুষের সিদ্ধান্ত দরকার</div>
            <div class="sub">abstention ${pct(s.abstention_rate)}</div></div>
        </div>

        <div class="mt">
          <div class="bar">${bars.map(([k, v]) =>
            `<i class="${k}" style="width:${(v / total * 100).toFixed(2)}%" title="${h(b_label(k))}: ${n(v)}"></i>`
          ).join('')}</div>
          <div class="legend">${bars.map(([k, v, lbl]) =>
            `<span><i class="bar-key" style="background:var(--${tone_var(k)})"></i>${h(lbl)} ${n(v)}</span>`
          ).join('')}</div>
        </div>

        <div class="row mt">
          <a href="#/audit/${run.id}"><button class="ghost" type="button">সব ফলাফল দেখুন</button></a>
          <a href="#/review"><button type="button">সিদ্ধান্তের সারিতে যান (${n((s.decisions?.MANUAL_REVIEW || 0) + (s.decisions?.SUGGEST_REPLACEMENT || 0))})</button></a>
        </div>
      </div>`;
    } else {
      audit = `<div class="card"><h2>এখনো কোনো নিরীক্ষা চালানো হয়নি</h2>
        <p class="hint">ম্যাপিং গুলো বর্তমান release-এর বিপরীতে যাচাই করতে একটি নিরীক্ষা চালান।</p>
        <a href="#/audit"><button type="button">নিরীক্ষা চালান</button></a></div>`;
    }

    // -- what is loaded
    const byS = {};
    releases.forEach(r => { (byS[r.system] = byS[r.system] || []).push(r); });
    const loaded = `<div class="card">
      <h2>ডেটাবেসে যা আছে</h2>
      <p class="hint">পুরনো release কখনো মুছে ফেলা হয় না — শুধু "current" পতাকাটা সরে যায়।</p>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Terminology</th><th>সংস্করণ</th><th>অবস্থা</th><th>import</th><th>ফাইল</th></tr></thead>
        <tbody>${releases.map(r => `<tr>
          <td>${h(r.system)}</td>
          <td class="mono">${h(r.version)}</td>
          <td>${r.is_current ? '<span class="pill keep">current</span>' : '<span class="pill neutral">superseded</span>'}</td>
          <td class="small">${when(r.imported_at)}</td>
          <td class="mono small faint">${h(r.source_filename)}</td>
        </tr>`).join('')}</tbody></table></div></div>`;

    view().innerHTML = head + rel + '<div style="height:15px"></div>' + audit + loaded;
    refreshBadge();
  } catch (e) { failed(e, 'ড্যাশবোর্ড লোড করা গেল না'); }
};

function tone_var(k) {
  return { keep: 'ok', warning: 'warn', suggest: 'accent', review: 'review', unknown: 'bad' }[k] || 'muted';
}
function b_label(k) {
  return { keep: 'ঠিক আছে', warning: 'সতর্কতা', suggest: 'বিকল্প আছে', review: 'মানুষ দেখুন', unknown: 'অজানা' }[k] || k;
}

// ==================================================================== LOOKUP
ROUTES.lookup = async (rest) => {
  const sys = rest[0] || 'LOINC';
  const code = rest[1] ? decodeURIComponent(rest[1]) : '';

  view().innerHTML = `<div class="page-head">
      <h1>কোড দেখুন</h1>
      <p class="lede">একটি LOINC কোড বা SNOMED concept id দিন। উত্তরটি সবসময় বলবে —
      <b>কোন release-এর বিপরীতে</b> বিচার হলো, কোডটির অবস্থা কী, আর যন্ত্রের সিদ্ধান্ত কী।</p></div>

    <div class="card">
      <div class="row">
        <div class="narrow">
          <label class="f" for="lkSys">Terminology</label>
          <select id="lkSys">
            <option value="LOINC" ${sys === 'LOINC' ? 'selected' : ''}>LOINC</option>
            <option value="SNOMED_CT" ${sys !== 'LOINC' ? 'selected' : ''}>SNOMED CT</option>
          </select>
        </div>
        <div style="flex:2 1 260px">
          <label class="f" for="lkCode">কোড</label>
          <input type="text" id="lkCode" value="${h(code)}" placeholder="যেমন 5895-7  বা  57371010000105" autocomplete="off">
        </div>
        <div class="narrow"><button id="lkGo" type="button">দেখুন</button></div>
      </div>
      <p class="hint mt mb0">চেষ্টা করে দেখুন:
        <a href="#/lookup/LOINC/5895-7">5895-7</a> (পরিত্যক্ত, বিকল্প আছে) ·
        <a href="#/lookup/LOINC/2531-2">2531-2</a> (দুটি বিকল্প — যন্ত্র হাত তুলে দেয়) ·
        <a href="#/lookup/LOINC/2951-2">2951-2</a> (দিব্যি বৈধ) ·
        <a href="#/lookup/SNOMED_CT/57371010000105">57371010000105</a> (নিষ্ক্রিয় SNOMED)
      </p>
    </div>
    <div id="lkOut"></div>`;

  const run = () => {
    const s = $('#lkSys').value, c = $('#lkCode').value.trim();
    if (c) location.hash = `#/lookup/${s}/${encodeURIComponent(c)}`;
  };
  $('#lkGo').addEventListener('click', run);
  $('#lkCode').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });

  if (!code) return;
  $('#lkOut').innerHTML = `<div class="empty"><span class="spin"></span></div>`;
  try {
    const path = sys === 'LOINC'
      ? `/api/v1/loinc/${encodeURIComponent(code)}/resolve`
      : `/api/v1/snomed/${encodeURIComponent(code)}/resolve`;
    const r = await api(path);
    $('#lkOut').innerHTML = verdictCard(r, sys);
  } catch (e) {
    $('#lkOut').innerHTML = `<div class="note bad"><p>${h(e.message)}</p></div>`;
  }
};

function verdictCard(r, sys) {
  const d = dec(r.decision);
  const code = r.code || r.concept_id;
  const targets = r.suggested_targets || [];

  let extra = '';
  if (sys === 'LOINC') {
    extra = `<dt>LOINC status</dt><dd class="mono">${dash(r.raw_status)}</dd>`;
    if (r.metadata_changed) {
      const diff = r.metadata_diff || {};
      extra += `<dt>metadata</dt><dd>কোড বদলায়নি, কিন্তু ${Object.keys(diff).length}টি ক্ষেত্র বদলেছে</dd>`;
    }
  } else {
    extra = `<dt>active</dt><dd>${r.active === null ? '—' : (r.active ? 'হ্যাঁ' : 'না')}</dd>`;
    if (r.inactivation_reason) {
      extra += `<dt>নিষ্ক্রিয় কেন</dt><dd class="mono">${h(r.inactivation_reason)}</dd>`;
    }
  }

  const assoc = (r.historical_associations || []).length
    ? `<div class="card"><h2>Historical association</h2>
       <p class="hint">SNOMED নিজেই যে সম্পর্কগুলো ঘোষণা করেছে।</p>
       <div class="tbl-wrap"><table class="tbl">
         <thead><tr><th>ধরন</th><th>লক্ষ্য</th><th>লক্ষ্যটি active?</th></tr></thead>
         <tbody>${r.historical_associations.map(a => `<tr>
           <td><span class="chip">${h(a.association_type)}</span></td>
           <td class="mono">${h(a.target_component_id)}</td>
           <td>${a.target_active === null ? '<span class="faint">জানা নেই</span>' : (a.target_active ? 'হ্যাঁ' : 'না')}</td>
         </tr>`).join('')}</tbody></table></div></div>`
    : '';

  const metaDiff = (sys === 'LOINC' && r.metadata_changed && r.metadata_diff)
    ? `<div class="card"><h2>কী কী বদলেছে</h2>
       <p class="hint">কোডটি একই আছে — শুধু বর্ণনা হালনাগাদ হয়েছে। কোড বদলানো <b>হবে না</b>।</p>
       <div class="tbl-wrap"><table class="tbl">
         <thead><tr><th>ক্ষেত্র</th><th>আগে (${h(r.details?.mapped_against_version || 'পুরনো')})</th><th>এখন (${h(r.version)})</th></tr></thead>
         <tbody>${Object.entries(r.metadata_diff).map(([k, v]) => `<tr>
           <td class="mono">${h(k)}</td><td class="strike">${dash(v.old ?? v[0])}</td><td>${dash(v.new ?? v[1])}</td>
         </tr>`).join('')}</tbody></table></div></div>`
    : '';

  return `<div class="verdict ${d.tone}">
      <div class="top">
        <span class="code mono">${h(code)}</span>
        ${pill(r.decision)} ${statPill(r.status)}
        <span class="chip">যাচাই হয়েছে ${h(r.system)} ${h(r.version || '—')}-এর বিপরীতে</span>
      </div>
      ${r.display ? `<p class="display">${h(r.display)}</p>` : ''}
      <p class="plain">${h(d.say)}${r.reason && REASON[r.reason] ? ' ' + h(REASON[r.reason]) : ''}</p>
      <dl>
        <dt>সিদ্ধান্ত</dt><dd class="mono">${h(r.decision)}</dd>
        <dt>কারণ</dt><dd class="mono">${dash(r.reason)}</dd>
        ${extra}
      </dl>
      ${r.details?.message ? `<div class="note mt mb0"><p>${h(r.details.message)}</p></div>` : ''}
    </div>

    ${targets.length ? `<div class="card mt">
      <h2>প্রস্তাবিত বিকল্প (${targets.length})</h2>
      <p class="hint">${targets.length > 1
        ? 'একাধিক থাকায় যন্ত্র <b>নিজে বাছাই করেনি</b> — কোনটি ঠিক তা স্থানীয় পরীক্ষার প্রেক্ষাপটে নির্ভর করে।'
        : 'terminology-র নিজের ঘোষিত উত্তরসূরি।'}</p>
      <div class="grid c2">${targets.map(t => candCard(t, sys)).join('')}</div>
    </div>` : ''}
    ${assoc}${metaDiff}`;
}

function candCard(t, sys) {
  const code = t.code || t.concept_id;
  const ok = !!t.usable;
  const link = sys === 'LOINC' ? `#/lookup/LOINC/${encodeURIComponent(code)}`
                               : `#/lookup/SNOMED_CT/${encodeURIComponent(code)}`;
  return `<div class="cand ${ok ? 'usable' : 'unusable'}">
    <div class="c-code mono"><a href="${link}">${h(code)}</a>
      ${ok ? '<span class="pill keep">ব্যবহারযোগ্য</span>' : '<span class="pill unknown">ব্যবহারযোগ্য নয়</span>'}</div>
    ${t.display ? `<div class="c-disp">${h(t.display)}</div>` : ''}
    <div class="c-via">
      ${t.status ? 'status ' + h(t.status) + ' · ' : ''}
      ${t.association_type ? h(t.association_type) + ' · ' : ''}
      ${(t.via && t.via.length > 1) ? 'পথ: ' + t.via.map(h).join(' → ') : ''}
    </div>
    ${t.note ? `<div class="c-via">${h(t.note)}</div>` : ''}
  </div>`;
}

// ================================================================== MAPPINGS
ROUTES.mappings = async (rest) => {
  if (rest[0]) return mappingDetail(rest[0]);
  loading();
  try {
    const q = new URLSearchParams(location.search);
    const rows = await api('/api/v1/mappings?limit=500');
    const datasets = [...new Set(rows.map(r => r.source_dataset))].sort();

    view().innerHTML = `<div class="page-head">
        <h1>স্থানীয় ম্যাপিং</h1>
        <p class="lede">হাসপাতালের নিজস্ব পরীক্ষার নাম আর তার সাথে বসানো standard কোড।
        <b>কোন release-এর বিপরীতে</b> বসানো হয়েছিল সেটাও এখানে থাকে — না থাকলে
        <span class="faint">খালি</span> দেখাবে, অনুমান করা হবে না।</p></div>

      <div class="card">
        <div class="row">
          <div><label class="f" for="mFilter">খুঁজুন</label>
            <input type="text" id="mFilter" placeholder="নাম বা কোড লিখুন…" autocomplete="off"></div>
          <div class="narrow"><label class="f" for="mDs">Dataset</label>
            <select id="mDs"><option value="">সব</option>
              ${datasets.map(d => `<option>${h(d)}</option>`).join('')}</select></div>
          <div class="narrow"><label class="f" for="mSys">Terminology</label>
            <select id="mSys"><option value="">সব</option><option>LOINC</option><option>SNOMED_CT</option></select></div>
        </div>
        <p class="hint mt mb0" id="mCount"></p>
      </div>

      <div class="card"><div class="tbl-wrap"><table class="tbl">
        <thead><tr>
          <th>স্থানীয় কোড</th><th>স্থানীয় নাম</th><th>প্রেক্ষাপট</th>
          <th>Terminology</th><th>লক্ষ্য কোড</th><th>যে সংস্করণে বসানো</th><th>review</th>
        </tr></thead><tbody id="mBody"></tbody></table></div></div>`;

    const render = () => {
      const term = $('#mFilter').value.trim().toLowerCase();
      const ds = $('#mDs').value, sy = $('#mSys').value;
      const list = rows.filter(r =>
        (!ds || r.source_dataset === ds) &&
        (!sy || r.target_system === sy) &&
        (!term || r.local_text.toLowerCase().includes(term)
               || r.local_code.toLowerCase().includes(term)
               || (r.target_code || '').toLowerCase().includes(term)));
      $('#mCount').innerHTML = `${n(list.length)} টি দেখানো হচ্ছে (মোট ${n(rows.length)})`;
      $('#mBody').innerHTML = list.slice(0, 300).map(r => {
        const ctx = r.local_context_json || {};
        return `<tr class="clickable" onclick="location.hash='#/mappings/${r.id}'">
          <td class="mono">${h(r.local_code)}</td>
          <td>${h(r.local_text)}</td>
          <td class="small faint">${h([ctx.fluid, ctx.category].filter(Boolean).join(' · '))}</td>
          <td>${h(r.target_system)}</td>
          <td class="mono">${h(r.target_code)}</td>
          <td>${r.mapped_against_version ? '<span class="mono">' + h(r.mapped_against_version) + '</span>'
                                         : '<span class="faint">অজানা</span>'}</td>
          <td>${r.review_status === 'APPROVED' ? '<span class="pill keep">approved</span>'
              : r.review_status === 'NEEDS_REVIEW' ? '<span class="pill review">needs review</span>'
              : '<span class="chip">' + h(r.review_status) + '</span>'}</td>
        </tr>`;
      }).join('') || `<tr><td colspan="7" class="empty">কিছু মিলল না।</td></tr>`;
    };
    ['#mFilter', '#mDs', '#mSys'].forEach(s => {
      $(s).addEventListener('input', render); $(s).addEventListener('change', render);
    });
    render();
  } catch (e) { failed(e, 'ম্যাপিং তালিকা আনা গেল না'); }
};

async function mappingDetail(id) {
  loading();
  try {
    const m = await api(`/api/v1/mappings/${id}`);
    const revs = m.revisions || await api(`/api/v1/mappings/${id}/history`);
    const ctx = m.local_context_json || {};

    let live = '';
    try {
      const path = m.target_system === 'LOINC'
        ? `/api/v1/loinc/${encodeURIComponent(m.target_code)}/resolve`
        : `/api/v1/snomed/${encodeURIComponent(m.target_code)}/resolve`;
      const r = await api(path + (m.mapped_against_version
        ? `?mapped_against_version=${encodeURIComponent(m.mapped_against_version)}` : ''));
      live = `<h2 class="mt">এই কোডটি এখন কেমন আছে</h2>` + verdictCard(r, m.target_system);
    } catch { live = ''; }

    view().innerHTML = `<div class="page-head">
        <h1>${h(m.local_text)}</h1>
        <p class="lede"><span class="mono">${h(m.source_dataset)}</span> ·
          স্থানীয় কোড <span class="mono">${h(m.local_code)}</span></p></div>

      <div class="card"><h2>ম্যাপিং</h2>
        <dl class="kv">
          <dt>লক্ষ্য</dt><dd class="mono">${h(m.target_system)} ${h(m.target_code)}</dd>
          <dt>যে সংস্করণে বসানো</dt><dd>${m.mapped_against_version
            ? '<span class="mono">' + h(m.mapped_against_version) + '</span>'
            : '<span class="faint">অজানা — MIMIC-III এর ক্ষেত্রে সত্যিই অজানা, তাই বানানো হয়নি</span>'}</dd>
          <dt>সম্পর্কের ধরন</dt><dd class="mono">${h(m.map_correlation)}</dd>
          <dt>review</dt><dd>${h(m.review_status)}</dd>
          <dt>প্রেক্ষাপট</dt><dd>${h([ctx.fluid, ctx.category].filter(Boolean).join(' · ')) || '—'}</dd>
          <dt>তৈরি</dt><dd>${when(m.created_at)}</dd>
        </dl></div>

      <div class="card"><h2>ইতিহাস (${revs.length})</h2>
        <p class="hint">এই তালিকায় কিছু কখনো মুছে যায় না। কোড বদলালে নতুন সারি যোগ হয়।</p>
        ${revs.length ? `<div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>কখন</th><th>আগে</th><th>পরে</th><th>কে</th><th>কারণ</th></tr></thead>
          <tbody>${revs.map(v => `<tr>
            <td class="small">${when(v.approved_at || v.created_at)}</td>
            <td class="mono">${h(v.old_target_code)} <span class="faint">@${h(v.old_target_version || '—')}</span></td>
            <td class="mono">${h(v.new_target_code)} <span class="faint">@${h(v.new_target_version || '—')}</span></td>
            <td>${dash(v.approved_by)}</td>
            <td class="small">${dash(v.reason)}</td></tr>`).join('')}
          </tbody></table></div>`
        : `<div class="empty"><p>এখনো কোনো পরিবর্তন হয়নি।</p></div>`}
      </div>
      ${live}
      <a href="#/mappings"><button class="quiet" type="button">← তালিকায় ফিরুন</button></a>`;
  } catch (e) { failed(e, 'ম্যাপিংটি আনা গেল না'); }
}

// ===================================================================== AUDIT
ROUTES.audit = async (rest) => {
  if (rest[0]) return auditDetail(rest[0]);
  loading();
  try {
    const runs = await api('/api/v1/audits?limit=25');
    view().innerHTML = `<div class="page-head">
        <h1>নিরীক্ষা</h1>
        <p class="lede">প্রতিটি ম্যাপিং বর্তমান release-এর বিপরীতে যাচাই করা হয়।
        নিরীক্ষা <b>কখনো কোনো কোড বদলায় না</b> — শুধু রায় লেখে।</p></div>

      <div class="card">
        <h2>নতুন নিরীক্ষা চালান</h2>
        <p class="hint">খালি রাখলে সব ম্যাপিং যাচাই হবে।</p>
        <div class="row">
          <div><label class="f" for="aDs">Dataset (ঐচ্ছিক)</label>
            <input type="text" id="aDs" placeholder="যেমন MIMIC_III" autocomplete="off"></div>
          <div class="narrow"><label class="f" for="aSys">Terminology</label>
            <select id="aSys"><option value="">সব</option><option>LOINC</option><option>SNOMED_CT</option></select></div>
          <div class="narrow"><label class="f" for="aLim">সর্বোচ্চ কতটি</label>
            <input type="number" id="aLim" min="1" placeholder="সব"></div>
          <div class="narrow"><button id="aGo" type="button">চালান</button></div>
        </div>
      </div>

      <div class="card"><h2>আগের নিরীক্ষা</h2>
        ${runs.length ? `<div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>#</th><th>কখন</th><th>LOINC</th><th>SNOMED</th><th class="num">ম্যাপিং</th>
            <th class="num">বাসি</th><th class="num">মানুষ দরকার</th><th>scope</th></tr></thead>
          <tbody>${runs.map(r => {
            const s = r.summary_json || {};
            const stale = (s.discouraged || 0) + (s.deprecated || 0) + (s.inactive_snomed || 0);
            return `<tr class="clickable" onclick="location.hash='#/audit/${r.id}'">
              <td>${r.id}</td><td class="small">${when(r.started_at)}</td>
              <td class="mono">${dash(r.loinc_version)}</td><td class="mono">${dash(r.snomed_version)}</td>
              <td class="num">${n(r.mapping_count)}</td>
              <td class="num">${stale ? '<b>' + n(stale) + '</b>' : '0'}</td>
              <td class="num">${n(s.manual_review_required || 0)}</td>
              <td class="small faint">${h(r.scope_json?.source_dataset || 'সব')}</td></tr>`;
          }).join('')}</tbody></table></div>`
        : `<div class="empty"><p>এখনো কিছু নেই।</p></div>`}</div>`;

    $('#aGo').addEventListener('click', async () => {
      const btn = $('#aGo'); btn.disabled = true; btn.innerHTML = 'চলছে… <span class="spin"></span>';
      try {
        const body = {
          source_dataset: $('#aDs').value.trim() || null,
          target_system: $('#aSys').value || null,
          limit: $('#aLim').value ? Number($('#aLim').value) : null,
          export_csv: true,
        };
        const run = await api('/api/v1/audits', { method: 'POST', body: JSON.stringify(body) });
        toast(`নিরীক্ষা #${run.id} শেষ — ${n(run.mapping_count)} টি ম্যাপিং যাচাই হয়েছে।`, 'ok');
        location.hash = `#/audit/${run.id}`;
      } catch (e) {
        toast('নিরীক্ষা চালানো গেল না: ' + h(e.message), 'bad');
        btn.disabled = false; btn.textContent = 'চালান';
      }
    });
  } catch (e) { failed(e, 'নিরীক্ষার তালিকা আনা গেল না'); }
};

async function auditDetail(id, decision = '') {
  loading();
  try {
    const run = await api(`/api/v1/audits/${id}`);
    const s = run.summary_json || {};
    const [results, mappings] = await Promise.all([
      api(`/api/v1/audits/${id}/results?limit=500`
        + (decision ? `&decision=${encodeURIComponent(decision)}` : '')),
      api('/api/v1/mappings?limit=1000'),
    ]);
    const byId = new Map(mappings.map(m => [m.id, m]));

    const counts = s.decisions || {};
    const tabs = ['', 'KEEP', 'SUGGEST_REPLACEMENT', 'MANUAL_REVIEW', 'KEEP_WITH_WARNING', 'UNKNOWN_CODE']
      .filter(k => k === '' || counts[k])
      .map(k => `<button class="${k === decision ? 'on' : ''}" data-d="${k}">
          ${k === '' ? 'সব' : h(dec(k).bn)} <span class="chip">${n(k === '' ? run.mapping_count : counts[k])}</span>
        </button>`).join('');

    view().innerHTML = `<div class="page-head">
        <h1>নিরীক্ষা #${run.id}</h1>
        <p class="lede">${when(run.started_at)} · যাচাই হয়েছে
          <b>LOINC ${h(run.loinc_version || '—')}</b>${run.snomed_version ? ' ও <b>SNOMED ' + h(run.snomed_version) + '</b>' : ''}
          -এর বিপরীতে${run.scope_json?.source_dataset ? ' · dataset ' + h(run.scope_json.source_dataset) : ''}</p></div>

      <div class="grid c4">
        <div class="stat plain"><div class="n plain">${n(s.total_mappings)}</div><div class="k">যাচাই হয়েছে</div></div>
        <div class="stat ok"><div class="n">${n(s.valid)}</div><div class="k">এখনো বৈধ</div></div>
        <div class="stat warn"><div class="n">${n(s.discouraged)}</div><div class="k">নিরুৎসাহিত</div></div>
        <div class="stat bad"><div class="n">${n(s.deprecated)}</div><div class="k">পরিত্যক্ত</div></div>
      </div>
      <div class="grid c4 mt">
        <div class="stat"><div class="n">${n(s.single_replacement)}</div><div class="k">একটি বিকল্প</div></div>
        <div class="stat"><div class="n">${n(s.multiple_replacement)}</div><div class="k">একাধিক বিকল্প</div></div>
        <div class="stat"><div class="n">${n(s.no_replacement)}</div><div class="k">কোনো বিকল্প নেই</div></div>
        <div class="stat warn"><div class="n">${pct(s.abstention_rate)}</div><div class="k">abstention rate</div>
          <div class="sub">যন্ত্র কতবার হাত তুলেছে</div></div>
      </div>

      ${run.report_path ? `<div class="note mt"><p>CSV রিপোর্ট:
        <span class="mono small">${h(run.report_path)}</span></p></div>` : ''}

      <div class="card mt">
        <div class="tabs" id="aTabs">${tabs}</div>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>স্থানীয়</th><th>কোড</th><th>অবস্থা</th><th>সিদ্ধান্ত</th><th>কারণ</th><th>বিকল্প</th></tr></thead>
          <tbody>${results.map(r => {
            const md = r.metadata_json || {};
            const tg = r.suggested_targets_json || [];
            const m = byId.get(r.mapping_id);
            const ctx = (m && m.local_context_json) || {};
            return `<tr>
              <td>${h(m ? m.local_text : '')}
                <div class="faint small">${h([ctx.fluid, ctx.category].filter(Boolean).join(' · '))}</div>
                <div class="faint small mono">${h(m ? m.local_code : (r.mapping_id || ''))}</div></td>
              <td class="mono"><a href="#/lookup/${h(r.target_system)}/${encodeURIComponent(r.old_code)}">${h(r.old_code)}</a></td>
              <td>${statPill(r.terminology_status)}</td>
              <td>${pill(r.decision)}</td>
              <td class="small faint">${dash(r.reason)}</td>
              <td class="mono small">${tg.length
                ? tg.map(t => h(t.code || t.concept_id)).join('<br>')
                : '<span class="faint">—</span>'}</td></tr>`;
          }).join('') || `<tr><td colspan="6" class="empty">এই শ্রেণিতে কিছু নেই।</td></tr>`}
          </tbody></table></div>
        ${results.length >= 500 ? '<p class="hint mt mb0">প্রথম ৫০০টি দেখানো হচ্ছে।</p>' : ''}
      </div>
      <a href="#/audit"><button class="quiet" type="button">← নিরীক্ষার তালিকা</button></a>`;

    $('#aTabs').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      auditDetail(id, b.dataset.d);
    });
  } catch (e) { failed(e, 'নিরীক্ষাটি আনা গেল না'); }
}

// ==================================================================== REVIEW
ROUTES.review = async () => {
  loading('সিদ্ধান্তের সারি তৈরি হচ্ছে…');
  try {
    const runs = await api('/api/v1/audits?limit=1');
    if (!runs.length) {
      view().innerHTML = `<div class="page-head"><h1>সিদ্ধান্তের সারি</h1></div>
        <div class="card"><div class="empty"><div class="big">🧪</div>
        <p>এখনো কোনো নিরীক্ষা চালানো হয়নি, তাই সিদ্ধান্ত নেওয়ার কিছু নেই।</p>
        <a href="#/audit"><button type="button">নিরীক্ষা চালান</button></a></div></div>`;
      return;
    }
    const run = runs[0];
    const [sugg, manual, mappings] = await Promise.all([
      api(`/api/v1/audits/${run.id}/results?decision=SUGGEST_REPLACEMENT&limit=300`),
      api(`/api/v1/audits/${run.id}/results?decision=MANUAL_REVIEW&limit=300`),
      api('/api/v1/mappings?limit=1000'),
    ]);
    // The verdict knows the terminology's name for the code. The reviewer needs
    // the hospital's name for the test, and the specimen it was taken from --
    // that is the local context the engine abstained for want of.
    const byId = new Map(mappings.map(m => [m.id, m]));
    const items = [...sugg, ...manual].filter(r => r.mapping_id);

    view().innerHTML = `<div class="page-head">
        <h1>সিদ্ধান্তের সারি</h1>
        <p class="lede">নিরীক্ষা #${run.id} যেসব ম্যাপিং নিয়ে নিজে সিদ্ধান্ত নেয়নি বা
        পরিবর্তনের প্রস্তাব দিয়েছে।</p></div>

      <div class="note">
        <p><b>নিরাপত্তার নিয়ম:</b> যন্ত্র শুধু <i>প্রস্তাব</i> করতে পারে। কোড বদলাতে হলে
        <b>নামসহ একজন মানুষকে</b> অনুমোদন দিতে হয়। অনুমোদনের সময় পুরনো কোড ও তার
        release ইতিহাসে লেখা থাকে — কিছুই মুছে যায় না।</p>
      </div>

      <div class="card">
        <div class="row">
          <div><label class="f" for="rvName">আপনার নাম <span class="faint">(প্রতিটি অনুমোদনের সাথে লেখা থাকবে)</span></label>
            <input type="text" id="rvName" placeholder="যেমন dr-marzia" autocomplete="off"
              value="${h(localStorage.getItem('vas.reviewer') || '')}"></div>
          <div class="narrow"><label class="f" for="rvFilter">দেখান</label>
            <select id="rvFilter">
              <option value="">সব (${items.length})</option>
              <option value="SUGGEST_REPLACEMENT">বিকল্প আছে (${sugg.length})</option>
              <option value="MANUAL_REVIEW">মানুষ দেখুন (${manual.length})</option>
            </select></div>
        </div>
      </div>
      <div id="rvList"></div>`;

    $('#rvName').addEventListener('input', e =>
      localStorage.setItem('vas.reviewer', e.target.value.trim()));

    const render = () => {
      const f = $('#rvFilter').value;
      const list = items.filter(r => !f || r.decision === f);
      $('#rvList').innerHTML = list.length
        ? list.map(r => reviewCard(r, byId.get(r.mapping_id))).join('')
        : `<div class="card"><div class="empty"><div class="big">✅</div>
           <p>এই শ্রেণিতে সিদ্ধান্ত নেওয়ার কিছু বাকি নেই।</p></div></div>`;
    };
    $('#rvFilter').addEventListener('change', render);
    render();
  } catch (e) { failed(e, 'সিদ্ধান্তের সারি তৈরি করা গেল না'); }
};

function reviewCard(r, m) {
  const md = r.metadata_json || {};
  const tg = (r.suggested_targets_json || []);
  const usable = tg.filter(t => t.usable);
  const d = dec(r.decision);
  const ctx = (m && m.local_context_json) || {};
  const context = [ctx.fluid, ctx.category].filter(Boolean).join(' · ');

  const opts = usable.map(t =>
    `<option value="${h(t.code || t.concept_id)}">${h(t.code || t.concept_id)} — ${h((t.display || '').slice(0, 70))}</option>`
  ).join('');

  return `<div class="card" id="rc-${r.id}">
    <div class="top" style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">
      <b>${h(m ? m.local_text : 'ম্যাপিং ' + r.mapping_id)}</b>
      ${context ? `<span class="chip">${h(context)}</span>` : ''}
      ${pill(r.decision)} ${statPill(r.terminology_status)}
    </div>
    <p class="hint">
      ${m ? `<span class="mono">${h(m.source_dataset)}/${h(m.local_code)}</span> · ` : ''}
      বর্তমান কোড
      <a href="#/lookup/${h(r.target_system)}/${encodeURIComponent(r.old_code)}" class="mono">${h(r.old_code)}</a>
      ${md.display ? '· ' + h(md.display) : ''}
      · যাচাই <span class="mono">${h(r.current_version || '')}</span>-এর বিপরীতে</p>

    <p class="plain" style="background:var(--line-soft);padding:8px 10px;border-radius:6px">
      ${h(d.say)} ${r.reason && REASON[r.reason] ? h(REASON[r.reason]) : ''}</p>

    ${usable.length > 1 && context ? `<div class="note warn mt"><p>এই পরীক্ষাটি
       <b>${h(context)}</b> থেকে নেওয়া। নিচের বিকল্পগুলো সাধারণত পদ্ধতি বা নমুনায় আলাদা —
       আপনার ল্যাবের প্রকৃত পদ্ধতির সাথে মিলিয়ে বাছুন।</p></div>` : ''}
    ${tg.length ? `<div class="grid c2 mt">${tg.map(t => candCard(t, r.target_system)).join('')}</div>` : ''}

    ${usable.length ? `<div class="row mt">
        <div><label class="f">যে কোডে বদলাবেন</label>
          <select id="sel-${r.id}">${opts}</select></div>
        <div style="flex:2 1 220px"><label class="f">কারণ <span class="faint">(ঐচ্ছিক, ইতিহাসে থাকবে)</span></label>
          <input type="text" id="rsn-${r.id}" placeholder="যেমন: স্থানীয় পদ্ধতির সাথে মিলিয়ে দেখা হয়েছে"></div>
        <div class="narrow"><button type="button" onclick="approve(${r.id}, ${r.mapping_id})">অনুমোদন</button></div>
      </div>`
    : `<div class="note warn mt mb0"><p>ব্যবহারযোগ্য কোনো বিকল্প নেই — এখানে কোড বেছে দেওয়ার
       কিছু নেই। সঠিক কোডটি হাতে খুঁজে বের করতে হবে, তারপর
       <span class="mono">/api/v1/mappings/${r.mapping_id}/approve-replacement</span> দিয়ে
       <span class="mono">allow_unsuggested</span> সহ প্রয়োগ করতে হবে।</p></div>`}
  </div>`;
}

window.approve = async (resultId, mappingId) => {
  const reviewer = ($('#rvName').value || '').trim();
  if (!reviewer) {
    toast('আগে আপনার নাম লিখুন — অনুমোদন কার, সেটা রেকর্ডে থাকতে হয়।', 'bad');
    $('#rvName').focus();
    return;
  }
  const target = $(`#sel-${resultId}`).value;
  const reason = ($(`#rsn-${resultId}`).value || '').trim();

  modal('এই পরিবর্তনটি নিশ্চিত করুন', `
    <p>এটি প্রয়োগ করলে যা ঘটবে:</p>
    <dl class="kv">
      <dt>ম্যাপিং</dt><dd>#${mappingId}</dd>
      <dt>নতুন কোড</dt><dd class="mono">${h(target)}</dd>
      <dt>অনুমোদনকারী</dt><dd>${h(reviewer)}</dd>
      <dt>কারণ</dt><dd>${reason ? h(reason) : '<span class="faint">দেওয়া হয়নি</span>'}</dd>
    </dl>
    <div class="note mt"><p>পুরনো কোড ও তার release একটি নতুন ইতিহাস-সারিতে সংরক্ষিত থাকবে।
    কিছুই মুছে যাবে না। লক্ষ্য কোডটি বর্তমান release-এ বৈধ কিনা, সেটাও আবার যাচাই হবে।</p></div>
    <div class="row mt">
      <div class="narrow"><button type="button" id="okBtn">হ্যাঁ, অনুমোদন করুন</button></div>
      <div class="narrow"><button type="button" class="quiet" onclick="closeModal()">বাতিল</button></div>
    </div>`);

  $('#okBtn').addEventListener('click', async () => {
    const btn = $('#okBtn'); btn.disabled = true; btn.innerHTML = 'পাঠাচ্ছি… <span class="spin"></span>';
    try {
      await api(`/api/v1/mappings/${mappingId}/approve-replacement`, {
        method: 'POST',
        body: JSON.stringify({
          target_code: target, reviewer, reason: reason || null, audit_result_id: resultId,
          allow_unsuggested: false,
        }),
      });
      closeModal();
      toast(`ম্যাপিং #${mappingId} এখন <span class="mono">${h(target)}</span>-এ। ইতিহাসে লেখা হয়েছে।`, 'ok');
      const card = $(`#rc-${resultId}`);
      if (card) {
        card.style.opacity = '.55';
        card.insertAdjacentHTML('beforeend',
          `<div class="note ok mt mb0"><p>✔ অনুমোদিত — <b>${h(reviewer)}</b>।
           <a href="#/mappings/${mappingId}">ইতিহাস দেখুন</a></p></div>`);
        card.querySelectorAll('button, select, input').forEach(x => x.disabled = true);
      }
    } catch (e) {
      closeModal();
      toast('অনুমোদন প্রত্যাখ্যাত: ' + h(e.message), 'bad');
    }
  });
};

// =================================================================== COMPARE
ROUTES.compare = async (rest) => {
  loading();
  try {
    const releases = await api('/api/v1/releases');
    const bySys = {};
    releases.forEach(r => { (bySys[r.system] = bySys[r.system] || []).push(r.version); });

    view().innerHTML = `<div class="page-head">
        <h1>সংস্করণ তুলনা</h1>
        <p class="lede">দুটি release পাশাপাশি রেখে কী বদলেছে দেখুন। LOINC-এর ক্ষেত্রে
        আমাদের হিসাব <b>terminology-র নিজের change log</b>-এর সাথে মিলিয়েও দেখানো হয় —
        সেখানে <b>missed = 0</b> হওয়াই লক্ষ্য।</p></div>

      <div class="card">
        <div class="row">
          <div class="narrow"><label class="f" for="cSys">Terminology</label>
            <select id="cSys">${Object.keys(bySys).map(s => `<option>${h(s)}</option>`).join('')}</select></div>
          <div><label class="f" for="cOld">পুরনো</label><select id="cOld"></select></div>
          <div><label class="f" for="cNew">নতুন</label><select id="cNew"></select></div>
          <div class="narrow"><button id="cGo" type="button">তুলনা করুন</button></div>
        </div>
        <p class="hint mt mb0">দুটি পূর্ণ LOINC release তুলনা করতে কয়েক সেকেন্ড লাগে —
          দুই লাখেরও বেশি সারি পড়তে হয়।</p>
      </div>
      <div id="cOut"></div>`;

    const fill = () => {
      const vs = (bySys[$('#cSys').value] || []).slice().sort();
      $('#cOld').innerHTML = vs.map(v => `<option>${h(v)}</option>`).join('');
      $('#cNew').innerHTML = vs.map(v => `<option>${h(v)}</option>`).join('');
      if (vs.length > 1) { $('#cOld').value = vs[vs.length - 2]; $('#cNew').value = vs[vs.length - 1]; }
    };
    $('#cSys').addEventListener('change', fill);
    fill();

    // A comparison reached from the address bar is linkable and bookmarkable,
    // which matters when the number in it is going into a thesis.
    if (rest && rest.length >= 3) {
      $('#cSys').value = rest[0]; fill();
      $('#cOld').value = decodeURIComponent(rest[1]);
      $('#cNew').value = decodeURIComponent(rest[2]);
    }

    const run = async () => {
      const sys = $('#cSys').value, old = $('#cOld').value, nw = $('#cNew').value;
      if (old === nw) { toast('দুটি ভিন্ন সংস্করণ বাছুন।', 'bad'); return; }
      $('#cOut').innerHTML = `<div class="empty"><span class="spin"></span>
        <p>${h(old)} আর ${h(nw)} মিলিয়ে দেখছি…</p></div>`;
      try {
        const d = await api(`/api/v1/releases/diff?system=${encodeURIComponent(sys)}`
          + `&old=${encodeURIComponent(old)}&new=${encodeURIComponent(nw)}`);
        $('#cOut').innerHTML = diffCard(d);
      } catch (e) {
        $('#cOut').innerHTML = `<div class="note bad"><p>${h(e.message)}</p></div>`;
      }
    };

    $('#cGo').addEventListener('click', () => {
      const h2 = `#/compare/${$('#cSys').value}/${encodeURIComponent($('#cOld').value)}`
               + `/${encodeURIComponent($('#cNew').value)}`;
      if (location.hash !== h2) { location.hash = h2; } else { run(); }
    });

    if (rest && rest.length >= 3) run();
  } catch (e) { failed(e, 'release তালিকা আনা গেল না'); }
};

function diffCard(d) {
  const v = d.validation || {};
  const isLoinc = d.system === 'LOINC';
  const missed = v.missed_changes ?? null;

  const validation = (isLoinc && v.change_snapshot_available !== false) ? `
    <div class="card">
      <h2>আমাদের হিসাব বনাম terminology-র নিজের হিসাব</h2>
      <p class="hint">এটাই প্রধান শুদ্ধতার প্রমাণ। LOINC নিজেই লিখে রাখে কী বদলেছে —
        আমরা সেই তালিকার সাথে মিলিয়ে দেখি।</p>
      <div class="grid c4">
        <div class="stat"><div class="n plain">${n(v.official_changes)}</div><div class="k">তারা বলেছে</div></div>
        <div class="stat"><div class="n plain">${n(v.detected_changes)}</div><div class="k">আমরা পেয়েছি</div></div>
        <div class="stat ${missed === 0 ? 'ok' : 'bad'}"><div class="n">${n(missed)}</div>
          <div class="k">আমরা মিস করেছি</div><div class="sub">লক্ষ্য: 0</div></div>
        <div class="stat ${(v.unexpected_changes || 0) === 0 ? 'ok' : 'warn'}">
          <div class="n">${n(v.unexpected_changes)}</div><div class="k">আমরা বাড়িয়ে বলেছি</div></div>
      </div>
      ${missed === 0
        ? `<div class="note ok mt mb0"><p>✔ ঘোষিত ${n(v.official_changes)} টি পরিবর্তনের
           <b>প্রতিটি</b> ধরা পড়েছে — একটিও মিস নয়, একটিও বাড়তি নয়।</p></div>`
        : `<div class="note bad mt mb0"><p>${n(missed)} টি official পরিবর্তন ধরা পড়েনি।</p></div>`}
      ${Object.keys(v.unsupported_official_properties || {}).length ? `
        <p class="hint mt mb0">যে ক্ষেত্রগুলো আমরা model করি না (চুপচাপ ফেলে দেওয়া হয় না, গোনা হয়):
        ${Object.entries(v.unsupported_official_properties).map(([k, c]) =>
          `<span class="chip">${h(k)} ${n(c)}</span>`).join(' ')}</p>` : ''}
    </div>` : '';

  const trans = d.status_transitions || {};
  const fields = d.changes_by_field || {};

  return `<div class="grid c4">
      <div class="stat"><div class="n plain">${n(d.old_total)}</div><div class="k">${h(d.old_version)}-এ কোড</div></div>
      <div class="stat"><div class="n plain">${n(d.new_total)}</div><div class="k">${h(d.new_version)}-এ কোড</div></div>
      <div class="stat ok"><div class="n">${n(d.new_codes)}</div><div class="k">নতুন যোগ হয়েছে</div></div>
      <div class="stat ${(d.removed_codes || 0) === 0 ? 'ok' : 'bad'}">
        <div class="n">${n(d.removed_codes)}</div><div class="k">হারিয়ে গেছে</div>
        <div class="sub">${(d.removed_codes || 0) === 0 ? 'যেমনটা হওয়া উচিত' : 'অস্বাভাবিক!'}</div></div>
    </div>
    ${validation}
    <div class="grid c2">
      <div class="card mb0"><h2>অবস্থার পরিবর্তন</h2>
        <p class="hint">যাতায়াত দুদিকেই হয় — কোড ফিরেও আসে।</p>
        ${Object.keys(trans).length ? `<table class="tbl">
          <tbody>${Object.entries(trans).sort((a, b) => b[1] - a[1]).map(([k, c]) =>
            `<tr><td class="mono small">${h(k)}</td><td class="num">${n(c)}</td></tr>`).join('')}
          </tbody></table>` : '<p class="muted">কোনো অবস্থা বদলায়নি।</p>'}
      </div>
      <div class="card mb0"><h2>কোন ক্ষেত্রে কত পরিবর্তন</h2>
        <p class="hint">মোট ${n(d.total_field_changes)} টি, ${n(d.changed_codes)} টি কোডে।</p>
        ${Object.keys(fields).length ? `<table class="tbl">
          <tbody>${Object.entries(fields).sort((a, b) => b[1] - a[1]).map(([k, c]) =>
            `<tr><td class="mono small">${h(k)}</td><td class="num">${n(c)}</td></tr>`).join('')}
          </tbody></table>` : '<p class="muted">কিছু বদলায়নি।</p>'}
      </div>
    </div>`;
}

// ================================================================== RELEASES
ROUTES.releases = async () => {
  loading();
  try {
    const rows = await api('/api/v1/releases');
    view().innerHTML = `<div class="page-head">
        <h1>Release তালিকা</h1>
        <p class="lede">যা একবার import হয়েছে তা চিরকাল এখানে থাকে। নতুন release এলে
        পুরনোটা মুছে যায় না — শুধু "current" পতাকা সরে যায়। এর ওপরেই পুরো
        পুনরুৎপাদনযোগ্যতা দাঁড়িয়ে।</p></div>
      <div class="card"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Terminology</th><th>সংস্করণ</th><th>কার্যকর</th><th>অবস্থা</th>
          <th>import</th><th>ফাইল</th><th>SHA-256</th><th>যা ঢুকেছে</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td>${h(r.system)}</td>
          <td class="mono"><b>${h(r.version)}</b></td>
          <td class="small">${dash(r.effective_date)}</td>
          <td>${r.is_current ? '<span class="pill keep">current</span>' : '<span class="pill neutral">superseded</span>'}</td>
          <td class="small">${when(r.imported_at)}</td>
          <td class="mono small">${h(r.source_filename)}</td>
          <td class="mono small faint" title="${h(r.sha256)}">${h(r.sha256.slice(0, 16))}…</td>
          <td class="small faint">${dash(r.notes)}</td>
        </tr>`).join('')}</tbody></table></div></div>

      <div class="note"><p><b>SHA-256 কেন?</b> ফাইলের নাম বদলে দিলেও এই ছাপ বদলায় না,
      আর ভেতরে একটা অক্ষর বদলালেই পুরো ছাপ বদলে যায়। তাই একই ডেটা দুবার import হওয়া
      অসম্ভব — নাম যা-ই হোক।</p></div>`;
  } catch (e) { failed(e, 'release তালিকা আনা গেল না'); }
};

// ====================================================================== HELP
ROUTES.help = () => {
  const rows = Object.entries(DECISION).map(([k, d]) => `<tr>
      <td>${pill(k)}</td><td class="mono small">${h(k)}</td><td>${h(d.say)}</td></tr>`).join('');
  const st = Object.entries(STATUS).map(([k, s]) => `<tr>
      <td>${statPill(k)}</td><td class="mono small">${h(k)}</td></tr>`).join('');
  const rs = Object.entries(REASON).map(([k, v]) => `<tr>
      <td class="mono small nowrap">${h(k)}</td><td>${h(v)}</td></tr>`).join('');

  view().innerHTML = `<div class="page-head">
      <h1>শব্দের মানে</h1>
      <p class="lede">এই কনসোল যেসব যান্ত্রিক শব্দ দেখায়, তার প্রতিটির অর্থ।</p></div>

    <div class="card"><h2>যন্ত্র যে পাঁচটি সিদ্ধান্ত নিতে পারে</h2>
      <p class="hint">এর বাইরে সে কিছু বলতে পারে না — ইচ্ছাকৃতভাবে।</p>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>সিদ্ধান্ত</th><th>API-তে</th><th>মানে</th></tr></thead>
        <tbody>${rows}</tbody></table></div></div>

    <div class="card"><h2>কোডের অবস্থা</h2>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>অবস্থা</th><th>API-তে</th></tr></thead><tbody>${st}</tbody></table></div></div>

    <div class="card"><h2>কারণের কোড</h2>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>কারণ</th><th>মানে</th></tr></thead><tbody>${rs}</tbody></table></div></div>

    <div class="card"><h2>তিনটি নিয়ম যা কখনো ভাঙা হয় না</h2>
      <ol>
        <li><b>নিশ্চিত না হলে যন্ত্র চুপ করে যায়।</b> দুটি সম্ভাব্য উত্তর থাকলে সে বাছে না —
          <span class="mono">MANUAL_REVIEW</span> বলে। ভুল উত্তরের চেয়ে "জানি না" ভালো।</li>
        <li><b>যন্ত্র নিজে থেকে কিছু বদলায় না।</b> নিরীক্ষা শুধু রায় লেখে। কোড বদলাতে
          নামসহ একজন মানুষের অনুমোদন লাগে।</li>
        <li><b>ইতিহাস কখনো মোছে না।</b> কোড বদলালে পুরনোটা ও তার release একটি নতুন
          সারিতে থেকে যায়। ছয় মাস পরেও বলা যায় কোন সিদ্ধান্ত কীসের ভিত্তিতে হয়েছিল।</li>
      </ol></div>

    <div class="card"><h2>"কোন সংস্করণের বিপরীতে" কথাটা কেন সবখানে</h2>
      <p>একটা কোড <i>নিজে থেকে</i> বৈধ বা অবৈধ নয় — সে কোনো একটা নির্দিষ্ট release-এ
      বৈধ বা অবৈধ। তাই এই কনসোল কোনো রায় দেখায় না যতক্ষণ না তার সাথে release-টির নামও
      দেখানো যায়। ২০২৬ সালে ঠিক থাকা একটা উত্তর ২০২৭-এ ভুল হতে পারে, আর সেটা লুকিয়ে
      রাখলে পুরো ব্যবস্থাটাই অর্থহীন।</p></div>`;
};

// ------------------------------------------------------------------ startup
async function refreshBadge() {
  try {
    const runs = await api('/api/v1/audits?limit=1');
    const s = runs[0]?.summary_json;
    const pending = s ? (s.decisions?.MANUAL_REVIEW || 0) + (s.decisions?.SUGGEST_REPLACEMENT || 0) : 0;
    const b = $('#reviewBadge');
    if (pending) { b.textContent = pending; b.hidden = false; } else { b.hidden = true; }
  } catch { /* the badge is a nicety; never let it break the page */ }
}

async function health() {
  const line = $('#healthLine');
  try {
    const hh = await api('/health');
    const ok = hh.status === 'ok';
    line.innerHTML = `<span class="pill ${ok ? 'keep' : 'warning'}">${ok ? 'সংযুক্ত' : h(hh.status)}</span>`
      + `<div class="mt small">DB ${hh.database ? '✔' : '✘'}`
      + ` · Snowstorm ${hh.snowstorm?.available ? '✔' : '<span title="term search unavailable">—</span>'}</div>`;
  } catch (e) {
    line.innerHTML = `<span class="pill unknown">সংযোগ নেই</span>`;
  }
}

$('#themeBtn').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
  if (next) { document.documentElement.setAttribute('data-theme', next); localStorage.setItem('vas.theme', next); }
  else { document.documentElement.removeAttribute('data-theme'); localStorage.removeItem('vas.theme'); }
});
const savedTheme = localStorage.getItem('vas.theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);

health();
refreshBadge();
go();
