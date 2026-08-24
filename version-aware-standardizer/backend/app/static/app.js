/* Terminology Console -- a small hash-routed app over the project's own REST API.
   Vanilla JS on purpose: no build step, no CDN, works with the network cable out.

   The one idea the whole UI is built around: a verdict is never shown as a bare
   code. It always carries the status, the decision, the release it was judged
   against, and a sentence saying what that means -- because a decision without
   its release is exactly the ambiguity this project exists to remove.

   The writing rule for everything a person reads here: say what happened, then
   say why it matters. Never make someone look up a word to understand a
   sentence. Introduce a technical term once, in context, and only where the
   plain word would be less precise. */

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
const plural = (c, one, many) => `${n(c)} ${c === 1 ? one : (many || one + 's')}`;

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
      <button class="quiet sm close" onclick="closeModal()">Close</button>
      <h3>${title}</h3>${inner}</div></div>`;
  $('#mh').addEventListener('click', e => { if (e.target.id === 'mh') closeModal(); });
}
function closeModal() { $('#modalHost').innerHTML = ''; }
window.closeModal = closeModal;

function loading(msg = 'Loading…') {
  view().innerHTML = `<div class="empty"><span class="spin"></span><p>${h(msg)}</p></div>`;
}

function failed(err, what) {
  view().innerHTML = `<div class="page-head"><h1>That didn't work</h1></div>
    <div class="note bad"><p><b>${h(what)}</b></p><p class="mono">${h(err.message)}</p>
    ${err.status === 503
      ? '<p>This usually means a terminology release has not been loaded yet. '
        + 'Check <a href="#/releases">Versions loaded</a>.</p>' : ''}</div>`;
}

// ------------------------------------------------------------ the vocabulary
// Every machine word the API can return, paired with the plain sentence that
// explains it. Kept in one place so the interface can never quietly invent a
// meaning the engine does not actually hold.
const DECISION = {
  KEEP: {
    tone: 'keep', label: 'Still valid', api: 'KEEP',
    say: 'This code is still good in the current release. Nothing to do.',
  },
  KEEP_WITH_WARNING: {
    tone: 'warning', label: 'Valid, but provisional', api: 'KEEP_WITH_WARNING',
    say: 'This code is marked TRIAL — published, but still provisional and liable to '
       + 'change. Safe to keep using; worth knowing about.',
  },
  SUGGEST_REPLACEMENT: {
    tone: 'suggest', label: 'Replacement available', api: 'SUGGEST_REPLACEMENT',
    say: 'This code should no longer be used, and the terminology itself names exactly '
       + 'one successor. We can propose it — but nothing changes until a person approves it.',
  },
  MANUAL_REVIEW: {
    tone: 'review', label: 'Needs a person', api: 'MANUAL_REVIEW',
    say: 'We deliberately did not decide. When there is more than one honest answer, '
       + 'guessing is worse than asking.',
  },
  UNKNOWN_CODE: {
    tone: 'unknown', label: 'Not recognised', api: 'UNKNOWN_CODE',
    say: 'This code is not in the current release at all — usually a typo, a code from a '
       + 'different terminology, or one that was never valid.',
  },
};

const STATUS = {
  CURRENT_VALID: { tone: 'keep', label: 'Active', say: 'Fine to use for new mappings.' },
  CURRENT_TRIAL: { tone: 'warning', label: 'Trial', say: 'Published but provisional; it may still change.' },
  DISCOURAGED: { tone: 'warning', label: 'Discouraged', say: 'Still resolves, but you are asked not to use it any more.' },
  DEPRECATED: { tone: 'unknown', label: 'Retired', say: 'Withdrawn. Never use it for a new mapping.' },
  INACTIVE: { tone: 'unknown', label: 'Inactive', say: 'Switched off in SNOMED CT; not for new data entry.' },
  UNKNOWN: { tone: 'unknown', label: 'Not found', say: 'Absent from the current release.' },
};

const REASON = {
  STATUS_ACTIVE: 'It is active in the current release.',
  STATUS_TRIAL: 'It is marked TRIAL in the current release.',
  SINGLE_OFFICIAL_REPLACEMENT: 'Exactly one official replacement is published.',
  MULTIPLE_REPLACEMENTS: 'Several official replacements exist, and which one is right '
    + 'depends on how your lab actually runs the test.',
  NO_OFFICIAL_REPLACEMENT: 'No replacement has been published at all — someone has to find one.',
  NO_HISTORICAL_ASSOCIATION: 'It is inactive, and no successor has been declared for it.',
  AMBIGUOUS_ASSOCIATION_TYPE: 'The relationship published for it is not definite enough to '
    + 'treat as a replacement.',
  REPLACEMENT_TARGET_NOT_CURRENT: 'The proposed successor is itself no longer valid.',
  REPLACEMENT_CHAIN_CYCLE: 'The chain of replacements loops back on itself.',
  REPLACEMENT_CHAIN_TOO_DEEP: 'The chain of replacements runs deeper than we will follow safely.',
  CODE_NOT_IN_CURRENT_RELEASE: 'The code does not appear in the current release.',
  NO_CURRENT_RELEASE: 'No release of this terminology has been loaded yet.',
  MOVED_TO_OTHER_NAMESPACE: 'It was moved to a different namespace — an administrative move, '
    + 'not a clinical replacement.',
};

const dec = d => DECISION[d] || { tone: 'neutral', label: d, api: d, say: '' };
const stat = s => STATUS[s] || { tone: 'neutral', label: s, say: '' };
const pill = d => `<span class="pill ${dec(d).tone}"><span class="dot"></span>${h(dec(d).label)}</span>`;
const statPill = s => `<span class="pill ${stat(s).tone}" title="${h(stat(s).say)}">${h(stat(s).label)}</span>`;
const sysName = s => (s === 'SNOMED_CT' ? 'SNOMED CT' : s);

// ------------------------------------------------------------------- router
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

// =============================================================== the primer
// Shown once, at the top of the dashboard, for someone who has never met this
// before. The dismissed flag lives in the browser, not the database -- it is a
// convenience, not a fact about the work.
function primer() {
  if (localStorage.getItem('vas.primerDismissed') === '1') return '';
  return `<div class="card" id="primer" style="border-inline-start:4px solid var(--accent)">
    <button class="quiet sm" style="float:inline-end" onclick="dismissPrimer()">Got it</button>
    <h2>New here? The whole idea, in four lines.</h2>
    <ol class="mb0">
      <li>Hospitals give every lab test a <b>standard code</b> so different systems can
        understand each other. LOINC names the tests; SNOMED CT names the findings.</li>
      <li>Those code lists are <b>republished regularly</b>, and codes get retired. But a
        retired code is never deleted — so it keeps working, quietly, long after it stopped
        being the right answer.</li>
      <li>This tool takes the mappings you already have and <b>re-checks every one against
        the release that is current today</b>.</li>
      <li>Where there is one official replacement, it says so. Where there is any doubt, it
        <b>stops and asks you</b> — and it never changes a code on its own.</li>
    </ol>
  </div>`;
}
window.dismissPrimer = () => {
  localStorage.setItem('vas.primerDismissed', '1');
  const el = $('#primer'); if (el) el.remove();
};

// ================================================================= DASHBOARD
ROUTES.dashboard = async () => {
  loading();
  try {
    const [health, releases, runs] = await Promise.all([
      api('/health'), api('/api/v1/releases'), api('/api/v1/audits?limit=5'),
    ]);

    const cur = health.releases || {};
    const run = runs[0];
    const s = run && run.summary_json ? run.summary_json : null;

    const head = `<div class="page-head">
      <h1>Dashboard</h1>
      <p class="lede">Which terminology versions this system is speaking right now, and what
      the last check found.</p></div>`;

    let rel = '<div class="grid c2">';
    for (const [sys, title, blurb] of [
      ['LOINC', 'LOINC', 'Codes for laboratory tests and measurements.'],
      ['SNOMED_CT', 'SNOMED CT', 'Codes for clinical findings, organisms and procedures.'],
    ]) {
      const r = cur[sys];
      rel += `<div class="card mb0">
        <h2>${title}</h2>
        <p class="hint">${blurb}</p>
        ${r ? `<div class="stat" style="border:0;padding:0">
            <div class="n">${h(r.version)}</div>
            <div class="k">${r.effective_date
              ? 'in force since ' + h(r.effective_date)
              : 'no effective date published'}</div>
          </div>
          <dl class="kv mt">
            <dt>From file</dt><dd class="mono small">${h(r.source_filename)}</dd>
            <dt>Loaded</dt><dd>${when(r.imported_at)}</dd>
            <dt>State</dt><dd>${h(r.import_status)}</dd>
            <dt title="A fingerprint of the file's contents. Renaming the file does not change it.">Fingerprint</dt>
            <dd class="mono small faint">${h(r.sha256.slice(0, 24))}…</dd>
          </dl>`
        : `<div class="note warn mb0"><p>Nothing loaded yet. Until a release is imported,
            this half of the system has nothing to check against.</p></div>`}
      </div>`;
    }
    rel += '</div>';

    let audit = '';
    if (s) {
      const bars = [
        ['keep', s.decisions?.KEEP || 0, 'Still valid'],
        ['warning', s.decisions?.KEEP_WITH_WARNING || 0, 'Provisional'],
        ['suggest', s.decisions?.SUGGEST_REPLACEMENT || 0, 'Replacement available'],
        ['review', s.decisions?.MANUAL_REVIEW || 0, 'Needs a person'],
        ['unknown', s.decisions?.UNKNOWN_CODE || 0, 'Not recognised'],
      ].filter(b => b[1] > 0);
      const total = s.total_mappings || 1;
      const stale = (s.discouraged || 0) + (s.deprecated || 0) + (s.inactive_snomed || 0);
      const pending = (s.decisions?.MANUAL_REVIEW || 0) + (s.decisions?.SUGGEST_REPLACEMENT || 0);

      audit = `<div class="card">
        <h2>Last check <span class="chip">run #${run.id}</span></h2>
        <p class="hint">${when(run.started_at)} · judged against LOINC ${h(run.loinc_version || '—')}
          ${run.snomed_version ? ' and SNOMED CT ' + h(run.snomed_version) : ''}
          ${run.scope_json?.source_dataset ? ' · source ' + h(run.scope_json.source_dataset) : ''}</p>

        <div class="grid c4">
          <div class="stat plain"><div class="n plain">${n(s.total_mappings)}</div>
            <div class="k">mappings checked</div></div>
          <div class="stat ok"><div class="n">${n(s.valid)}</div>
            <div class="k">still valid</div></div>
          <div class="stat bad"><div class="n">${n(stale)}</div>
            <div class="k">gone stale</div>
            <div class="sub">${pct(stale / total)} of the total</div></div>
          <div class="stat warn"><div class="n">${n(s.manual_review_required)}</div>
            <div class="k">we could not decide</div>
            <div class="sub">${pct(s.abstention_rate)} of the total</div></div>
        </div>

        <div class="mt">
          <div class="bar">${bars.map(([k, v, lbl]) =>
            `<i class="${k}" style="width:${(v / total * 100).toFixed(2)}%" title="${h(lbl)}: ${n(v)}"></i>`
          ).join('')}</div>
          <div class="legend">${bars.map(([k, v, lbl]) =>
            `<span><i style="background:var(--${toneVar(k)})"></i>${h(lbl)} ${n(v)}</span>`
          ).join('')}</div>
        </div>

        ${stale ? `<div class="note mt"><p><b>What this means.</b> ${plural(stale, 'mapping')}
          here point at a code that has since been retired or discouraged. Nothing in the data
          would have told you — a retired code still looks like a perfectly ordinary code.</p></div>` : ''}

        <div class="row mt">
          <div class="auto"><a href="#/audit/${run.id}"><button class="ghost" type="button">See all results</button></a></div>
          ${pending ? `<div class="auto"><a href="#/review"><button type="button">Review ${n(pending)} now</button></a></div>` : ''}
        </div>
      </div>`;
    } else {
      audit = `<div class="card"><h2>No check has been run yet</h2>
        <p class="hint">Once you have mappings loaded, run a check to see which of them are
        still valid against today's releases.</p>
        <a href="#/audit"><button type="button">Run the first check</button></a></div>`;
    }

    const loaded = `<div class="card">
      <h2>Versions in the database</h2>
      <p class="hint">Superseded versions are kept, never deleted — only the "in use now" flag
        moves. That is what makes an old result reproducible months later.</p>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Terminology</th><th>Version</th><th>State</th><th>Loaded</th><th>From file</th></tr></thead>
        <tbody>${releases.map(r => `<tr>
          <td>${h(sysName(r.system))}</td>
          <td class="mono">${h(r.version)}</td>
          <td>${r.is_current ? '<span class="pill keep">in use now</span>'
                             : '<span class="pill neutral">superseded</span>'}</td>
          <td class="small">${when(r.imported_at)}</td>
          <td class="mono small faint">${h(r.source_filename)}</td>
        </tr>`).join('')}</tbody></table></div></div>`;

    view().innerHTML = head + primer() + rel + '<div style="height:15px"></div>' + audit + loaded;
    refreshBadge();
  } catch (e) { failed(e, 'Could not load the dashboard'); }
};

function toneVar(k) {
  return { keep: 'ok', warning: 'warn', suggest: 'accent', review: 'review', unknown: 'bad' }[k] || 'muted';
}

// ==================================================================== LOOKUP
ROUTES.lookup = async (rest) => {
  const sys = rest[0] || 'LOINC';
  const code = rest[1] ? decodeURIComponent(rest[1]) : '';

  view().innerHTML = `<div class="page-head">
      <h1>Look up a code</h1>
      <p class="lede">Type a LOINC code or a SNOMED CT concept ID. Every answer names
      <b>the release it was judged against</b> — because a code is never valid or invalid on
      its own, only inside a particular version of the terminology.</p></div>

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
          <label class="f" for="lkCode">Code</label>
          <input type="text" id="lkCode" value="${h(code)}"
            placeholder="e.g. 5895-7   or   57371010000105" autocomplete="off" spellcheck="false">
        </div>
        <div class="auto"><button id="lkGo" type="button">Look it up</button></div>
      </div>
      <p class="hint mt mb0">Try a real example:
        <a href="#/lookup/LOINC/5895-7">5895-7</a> — retired, one clean replacement ·
        <a href="#/lookup/LOINC/2531-2">2531-2</a> — two replacements, so we stop and ask ·
        <a href="#/lookup/LOINC/2951-2">2951-2</a> — perfectly fine ·
        <a href="#/lookup/SNOMED_CT/57371010000105">57371010000105</a> — an inactive SNOMED concept
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
    $('#lkOut').innerHTML = verdictCard(await api(path), sys);
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
    extra = `<dt title="The status word LOINC itself publishes for this code">Published status</dt>
             <dd class="mono">${dash(r.raw_status)}</dd>`;
  } else {
    extra = `<dt>Active</dt><dd>${r.active === null ? '—' : (r.active ? 'yes' : 'no')}</dd>`;
    if (r.inactivation_reason) {
      extra += `<dt title="The reason SNOMED CT published for switching it off">Why it was switched off</dt>
                <dd class="mono">${h(r.inactivation_reason)}</dd>`;
    }
  }

  const assoc = (r.historical_associations || []).length
    ? `<div class="card"><h2>What SNOMED CT says it relates to</h2>
       <p class="hint">These relationships are published by SNOMED CT itself. Only
         <b>Replaced by</b> and <b>Same as</b> are definite enough to act on — the rest are
         shown for a person to weigh up.</p>
       <div class="tbl-wrap"><table class="tbl">
         <thead><tr><th>Relationship</th><th>Points at</th><th>Is that one active?</th></tr></thead>
         <tbody>${r.historical_associations.map(a => `<tr>
           <td><span class="chip">${h(prettyAssoc(a.association_type))}</span></td>
           <td class="mono">${h(a.target_component_id)}</td>
           <td>${a.target_active === null ? '<span class="faint">not loaded</span>'
                                          : (a.target_active ? 'yes' : 'no')}</td>
         </tr>`).join('')}</tbody></table></div></div>`
    : '';

  const metaDiff = (sys === 'LOINC' && r.metadata_changed && r.metadata_diff
                    && Object.keys(r.metadata_diff).length)
    ? `<div class="card"><h2>The description changed, but the code did not</h2>
       <p class="hint">Worth knowing, but <b>not</b> a reason to re-map anything. The code
         still means the same thing; the wording around it was updated.</p>
       <div class="tbl-wrap"><table class="tbl">
         <thead><tr><th>Field</th><th>Before</th><th>Now (${h(r.version)})</th></tr></thead>
         <tbody>${Object.entries(r.metadata_diff).map(([k, v]) => `<tr>
           <td>${h(prettyField(k))}</td>
           <td class="strike">${dash(v.old ?? v[0])}</td>
           <td>${dash(v.new ?? v[1])}</td>
         </tr>`).join('')}</tbody></table></div></div>`
    : '';

  return `<div class="verdict ${d.tone}">
      <div class="top">
        <span class="code mono">${h(code)}</span>
        ${pill(r.decision)} ${statPill(r.status)}
        <span class="chip">judged against ${h(sysName(r.system))} ${h(r.version || '—')}</span>
      </div>
      ${r.display ? `<p class="display">${h(r.display)}</p>` : ''}
      <p class="plain">${h(d.say)}${r.reason && REASON[r.reason] ? ' ' + h(REASON[r.reason]) : ''}</p>
      <dl>
        <dt>Verdict</dt><dd class="mono">${h(r.decision)}</dd>
        <dt>Because</dt><dd class="mono">${dash(r.reason)}</dd>
        ${extra}
      </dl>
      ${r.details?.message ? `<div class="note mt mb0"><p>${h(r.details.message)}</p></div>` : ''}
    </div>

    ${targets.length ? `<div class="card mt">
      <h2>${targets.length > 1 ? `Possible replacements (${targets.length})` : 'The official replacement'}</h2>
      <p class="hint">${targets.length > 1
        ? 'More than one is published, so <b>we did not pick</b>. Which is right depends on how '
          + 'your lab actually runs the test — usually the method or the specimen.'
        : 'This is the successor the terminology itself names.'}</p>
      <div class="grid c2">${targets.map(t => candCard(t, sys)).join('')}</div>
    </div>` : ''}
    ${assoc}${metaDiff}`;
}

function prettyAssoc(t) {
  return ({
    REPLACED_BY: 'Replaced by', SAME_AS: 'Same as',
    POSSIBLY_EQUIVALENT_TO: 'Possibly the same as', WAS_A: 'Used to be a kind of',
    ALTERNATIVE: 'Alternative', MOVED_TO: 'Moved to another namespace',
    MOVED_FROM: 'Moved from', REFERS_TO: 'Refers to', SIMILAR_TO: 'Similar to',
    PARTIALLY_EQUIVALENT_TO: 'Partly the same as',
  })[t] || t;
}

function candCard(t, sys) {
  const code = t.code || t.concept_id;
  const ok = !!t.usable;
  const link = `#/lookup/${sys === 'LOINC' ? 'LOINC' : 'SNOMED_CT'}/${encodeURIComponent(code)}`;
  const chain = (t.via && t.via.length > 2)
    ? `<div class="c-via">Reached through ${t.via.map(h).join(' → ')} — the first replacement
       was itself retired, so we followed the trail.</div>` : '';
  const statusWord = t.status
    ? ((STATUS[t.status === 'ACTIVE' ? 'CURRENT_VALID' : t.status] || {}).label || t.status)
    : '';
  return `<div class="cand ${ok ? 'usable' : 'unusable'}">
    <div class="c-code mono"><a href="${link}">${h(code)}</a>
      ${ok ? '<span class="pill keep">safe to use</span>'
           : '<span class="pill unknown">not safe to use</span>'}</div>
    ${t.display ? `<div class="c-disp">${h(t.display)}</div>` : ''}
    <div class="c-via">
      ${statusWord ? h(statusWord) + ' in the current release' : ''}
      ${t.association_type ? ' · ' + h(prettyAssoc(t.association_type)) : ''}
    </div>
    ${chain}
    ${t.note ? `<div class="c-via">${h(t.note)}</div>` : ''}
  </div>`;
}

// ================================================================== MAPPINGS
ROUTES.mappings = async (rest) => {
  if (rest[0]) return mappingDetail(rest[0]);
  loading();
  try {
    const rows = await api('/api/v1/mappings?limit=1000');
    const datasets = [...new Set(rows.map(r => r.source_dataset))].sort();

    view().innerHTML = `<div class="page-head">
        <h1>Your mappings</h1>
        <p class="lede">Each row pairs one of your own test names with a standard code somebody
        once chose for it. The column that matters most is <b>which release that choice was made
        against</b> — and where it is genuinely unknown, we leave it blank rather than invent
        one.</p></div>

      <div class="card">
        <div class="row">
          <div><label class="f" for="mFilter">Search</label>
            <input type="text" id="mFilter" placeholder="test name or code…" autocomplete="off"></div>
          <div class="narrow"><label class="f" for="mDs">Source</label>
            <select id="mDs"><option value="">All sources</option>
              ${datasets.map(d => `<option>${h(d)}</option>`).join('')}</select></div>
          <div class="narrow"><label class="f" for="mSys">Terminology</label>
            <select id="mSys"><option value="">Both</option><option value="LOINC">LOINC</option>
              <option value="SNOMED_CT">SNOMED CT</option></select></div>
        </div>
        <p class="hint mt mb0" id="mCount"></p>
      </div>

      <div class="card"><div class="tbl-wrap"><table class="tbl">
        <thead><tr>
          <th>Your code</th><th>Your name for it</th><th>Specimen</th>
          <th>Terminology</th><th>Standard code</th><th>Chosen against</th><th>Reviewed?</th>
        </tr></thead><tbody id="mBody"></tbody></table></div>
        <p class="hint mt mb0">Click any row for its full history.</p></div>`;

    const render = () => {
      const term = $('#mFilter').value.trim().toLowerCase();
      const ds = $('#mDs').value, sy = $('#mSys').value;
      const list = rows.filter(r =>
        (!ds || r.source_dataset === ds) &&
        (!sy || r.target_system === sy) &&
        (!term || r.local_text.toLowerCase().includes(term)
               || r.local_code.toLowerCase().includes(term)
               || (r.target_code || '').toLowerCase().includes(term)));
      $('#mCount').innerHTML = list.length === rows.length
        ? `Showing all ${n(rows.length)}.`
        : `Showing ${n(list.length)} of ${n(rows.length)}.`;
      $('#mBody').innerHTML = list.slice(0, 300).map(r => {
        const ctx = r.local_context_json || {};
        return `<tr class="clickable" onclick="location.hash='#/mappings/${r.id}'">
          <td class="mono">${h(r.local_code)}</td>
          <td>${h(r.local_text)}</td>
          <td class="small faint">${h([ctx.fluid, ctx.category].filter(Boolean).join(' · '))}</td>
          <td>${h(sysName(r.target_system))}</td>
          <td class="mono">${h(r.target_code)}</td>
          <td>${r.mapped_against_version
            ? '<span class="mono">' + h(r.mapped_against_version) + '</span>'
            : '<span class="faint" title="Genuinely unknown for this dataset. We do not guess.">not recorded</span>'}</td>
          <td>${r.review_status === 'APPROVED' ? '<span class="pill keep">approved</span>'
              : r.review_status === 'NEEDS_REVIEW' ? '<span class="pill review">needs review</span>'
              : '<span class="chip">not yet</span>'}</td>
        </tr>`;
      }).join('') || `<tr><td colspan="7" class="empty">Nothing matches that.</td></tr>`;
    };
    ['#mFilter', '#mDs', '#mSys'].forEach(s => {
      $(s).addEventListener('input', render); $(s).addEventListener('change', render);
    });
    render();
  } catch (e) { failed(e, 'Could not load your mappings'); }
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
      live = `<h2 class="mt">How that code is doing today</h2>` + verdictCard(r, m.target_system);
    } catch { live = ''; }

    view().innerHTML = `<div class="page-head">
        <h1>${h(m.local_text)}</h1>
        <p class="lede">From <span class="mono">${h(m.source_dataset)}</span>, your code
          <span class="mono">${h(m.local_code)}</span></p></div>

      <div class="card"><h2>The mapping</h2>
        <dl class="kv">
          <dt>Standard code</dt>
          <dd class="mono">${h(sysName(m.target_system))} ${h(m.target_code)}</dd>
          <dt>Chosen against</dt><dd>${m.mapped_against_version
            ? '<span class="mono">' + h(m.mapped_against_version) + '</span>'
            : '<span class="faint">Not recorded — genuinely unknown for this dataset, so we '
              + 'leave it blank instead of inventing one.</span>'}</dd>
          <dt title="How closely the local term and the standard code line up">Closeness of match</dt>
          <dd>${h(prettyCorrelation(m.map_correlation))}</dd>
          <dt>Reviewed</dt>
          <dd>${h(m.review_status === 'UNREVIEWED' ? 'not yet' : m.review_status.toLowerCase())}</dd>
          <dt>Specimen</dt><dd>${h([ctx.fluid, ctx.category].filter(Boolean).join(' · ')) || '—'}</dd>
          <dt>Added</dt><dd>${when(m.created_at)}</dd>
        </dl></div>

      <div class="card"><h2>History (${revs.length})</h2>
        <p class="hint">Nothing is ever removed from this list. If the code changes, a new row
          is added — so you can always see what it used to be and who changed it.</p>
        ${revs.length ? `<div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>When</th><th>Was</th><th>Became</th><th>Approved by</th><th>Why</th></tr></thead>
          <tbody>${revs.map(v => `<tr>
            <td class="small">${when(v.approved_at || v.created_at)}</td>
            <td class="mono">${h(v.old_target_code)} <span class="faint">in ${h(v.old_target_version || '—')}</span></td>
            <td class="mono">${h(v.new_target_code)} <span class="faint">in ${h(v.new_target_version || '—')}</span></td>
            <td>${dash(v.approved_by)}</td>
            <td class="small">${dash(v.reason)}</td></tr>`).join('')}
          </tbody></table></div>`
        : `<div class="empty"><p>This mapping has never been changed.</p></div>`}
      </div>
      ${live}
      <a href="#/mappings"><button class="quiet" type="button">← Back to all mappings</button></a>`;
  } catch (e) { failed(e, 'Could not load that mapping'); }
}

function prettyCorrelation(c) {
  return ({
    EXACT_MATCH: 'exact match',
    BROAD_TO_NARROW: 'the standard code is narrower than your term',
    NARROW_TO_BROAD: 'the standard code is broader than your term',
    PARTIAL_OVERLAP: 'they only partly overlap',
    NOT_SPECIFIED: 'not stated',
  })[c] || c;
}

// ===================================================================== AUDIT
ROUTES.audit = async (rest) => {
  if (rest[0]) return auditDetail(rest[0]);
  loading();
  try {
    const runs = await api('/api/v1/audits?limit=25');
    view().innerHTML = `<div class="page-head">
        <h1>Run a check</h1>
        <p class="lede">A check re-tests every mapping against the release that is current
        today. It <b>never changes a code</b> — it only records what it found. Changing
        anything is a separate, deliberate step.</p></div>

      <div class="card">
        <h2>New check</h2>
        <p class="hint">Leave everything blank to check all your mappings.</p>
        <div class="row">
          <div><label class="f" for="aDs">Only this source <span class="faint">(optional)</span></label>
            <input type="text" id="aDs" placeholder="e.g. MIMIC_III" autocomplete="off"></div>
          <div class="narrow"><label class="f" for="aSys">Only this terminology</label>
            <select id="aSys"><option value="">Both</option><option value="LOINC">LOINC</option>
              <option value="SNOMED_CT">SNOMED CT</option></select></div>
          <div class="narrow"><label class="f" for="aLim">Stop after</label>
            <input type="number" id="aLim" min="1" placeholder="no limit"></div>
          <div class="auto"><button id="aGo" type="button">Run it</button></div>
        </div>
      </div>

      <div class="card"><h2>Earlier checks</h2>
        ${runs.length ? `<div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>#</th><th>When</th><th>LOINC</th><th>SNOMED CT</th><th class="num">Checked</th>
            <th class="num">Stale</th><th class="num">Needed a person</th><th>Scope</th></tr></thead>
          <tbody>${runs.map(r => {
            const s = r.summary_json || {};
            const stale = (s.discouraged || 0) + (s.deprecated || 0) + (s.inactive_snomed || 0);
            return `<tr class="clickable" onclick="location.hash='#/audit/${r.id}'">
              <td>${r.id}</td><td class="small">${when(r.started_at)}</td>
              <td class="mono">${dash(r.loinc_version)}</td><td class="mono">${dash(r.snomed_version)}</td>
              <td class="num">${n(r.mapping_count)}</td>
              <td class="num">${stale ? '<b>' + n(stale) + '</b>' : '0'}</td>
              <td class="num">${n(s.manual_review_required || 0)}</td>
              <td class="small faint">${h(r.scope_json?.source_dataset || 'everything')}</td></tr>`;
          }).join('')}</tbody></table></div>`
        : `<div class="empty"><p>No checks yet.</p></div>`}</div>`;

    $('#aGo').addEventListener('click', async () => {
      const btn = $('#aGo'); btn.disabled = true; btn.innerHTML = 'Checking… <span class="spin"></span>';
      try {
        const run = await api('/api/v1/audits', {
          method: 'POST',
          body: JSON.stringify({
            source_dataset: $('#aDs').value.trim() || null,
            target_system: $('#aSys').value || null,
            limit: $('#aLim').value ? Number($('#aLim').value) : null,
            export_csv: true,
          }),
        });
        toast(`Check #${run.id} finished — ${plural(run.mapping_count, 'mapping')} tested.`, 'ok');
        location.hash = `#/audit/${run.id}`;
      } catch (e) {
        toast('The check could not run: ' + h(e.message), 'bad');
        btn.disabled = false; btn.textContent = 'Run it';
      }
    });
  } catch (e) { failed(e, 'Could not load earlier checks'); }
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
          ${k === '' ? 'Everything' : h(dec(k).label)}
          <span class="chip">${n(k === '' ? run.mapping_count : counts[k])}</span>
        </button>`).join('');

    view().innerHTML = `<div class="page-head">
        <h1>Check #${run.id}</h1>
        <p class="lede">${when(run.started_at)} · judged against
          <b>LOINC ${h(run.loinc_version || '—')}</b>${run.snomed_version
            ? ' and <b>SNOMED CT ' + h(run.snomed_version) + '</b>' : ''}
          ${run.scope_json?.source_dataset ? ' · source ' + h(run.scope_json.source_dataset) : ''}</p></div>

      <div class="grid c4">
        <div class="stat plain"><div class="n plain">${n(s.total_mappings)}</div><div class="k">checked</div></div>
        <div class="stat ok"><div class="n">${n(s.valid)}</div><div class="k">still valid</div></div>
        <div class="stat warn"><div class="n">${n(s.discouraged)}</div><div class="k">discouraged</div></div>
        <div class="stat bad"><div class="n">${n(s.deprecated)}</div><div class="k">retired</div></div>
      </div>
      <div class="grid c4 mt">
        <div class="stat"><div class="n">${n(s.single_replacement)}</div>
          <div class="k">one clear replacement</div></div>
        <div class="stat"><div class="n">${n(s.multiple_replacement)}</div>
          <div class="k">several to choose from</div></div>
        <div class="stat"><div class="n">${n(s.no_replacement)}</div>
          <div class="k">no replacement published</div></div>
        <div class="stat warn"><div class="n">${pct(s.abstention_rate)}</div>
          <div class="k">we stopped and asked</div>
          <div class="sub">the share we would not guess on</div></div>
      </div>

      ${run.report_path ? `<div class="note mt"><p>A spreadsheet of every result was saved to
        <span class="mono small">${h(run.report_path)}</span></p></div>` : ''}

      <div class="card mt">
        <div class="tabs" id="aTabs">${tabs}</div>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>Your test</th><th>Code</th><th>Status now</th><th>Verdict</th>
            <th>Because</th><th>Replacements</th></tr></thead>
          <tbody>${results.map(r => {
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
              <td class="small faint">${r.reason && REASON[r.reason] ? h(REASON[r.reason]) : dash(r.reason)}</td>
              <td class="mono small">${tg.length
                ? tg.map(t => h(t.code || t.concept_id)).join('<br>')
                : '<span class="faint">none</span>'}</td></tr>`;
          }).join('') || `<tr><td colspan="6" class="empty">Nothing in this category.</td></tr>`}
          </tbody></table></div>
        ${results.length >= 500 ? '<p class="hint mt mb0">Showing the first 500.</p>' : ''}
      </div>
      <a href="#/audit"><button class="quiet" type="button">← All checks</button></a>`;

    $('#aTabs').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      auditDetail(id, b.dataset.d);
    });
  } catch (e) { failed(e, 'Could not load that check'); }
}

// ==================================================================== REVIEW
ROUTES.review = async () => {
  loading('Gathering the cases that need you…');
  try {
    const runs = await api('/api/v1/audits?limit=1');
    if (!runs.length) {
      view().innerHTML = `<div class="page-head"><h1>Needs your decision</h1></div>
        <div class="card"><div class="empty"><div class="big">🧪</div>
        <p>No check has been run yet, so there is nothing to decide.</p>
        <a href="#/audit"><button type="button">Run a check first</button></a></div></div>`;
      return;
    }
    const run = runs[0];
    const [sugg, manual, mappings] = await Promise.all([
      api(`/api/v1/audits/${run.id}/results?decision=SUGGEST_REPLACEMENT&limit=300`),
      api(`/api/v1/audits/${run.id}/results?decision=MANUAL_REVIEW&limit=300`),
      api('/api/v1/mappings?limit=1000'),
    ]);
    // The verdict knows the terminology's name for the code. You need your own
    // name for the test, and the specimen it came from -- that is the local
    // context the engine stopped for want of.
    const byId = new Map(mappings.map(m => [m.id, m]));
    const items = [...sugg, ...manual].filter(r => r.mapping_id);

    view().innerHTML = `<div class="page-head">
        <h1>Needs your decision</h1>
        <p class="lede">From check #${run.id}: the mappings we either could not decide, or
        would like to change but will not without you.</p></div>

      <div class="note">
        <p><b>How this works.</b> The check can <i>propose</i>; only a person can <i>apply</i>.
        When you approve something, your name goes on it, the old code and the release it was
        valid in are written to the history, and nothing is erased. If the replacement is not
        valid in today's release, the approval is refused — even from here.</p>
      </div>

      <div class="card">
        <div class="row">
          <div><label class="f" for="rvName">Your name
              <span class="faint">— recorded against every approval you make</span></label>
            <input type="text" id="rvName" placeholder="e.g. Dr Marzia Khan" autocomplete="off"
              value="${h(localStorage.getItem('vas.reviewer') || '')}"></div>
          <div class="wide-narrow"><label class="f" for="rvFilter">Show</label>
            <select id="rvFilter">
              <option value="">Everything (${items.length})</option>
              <option value="SUGGEST_REPLACEMENT">Ready to approve (${sugg.length})</option>
              <option value="MANUAL_REVIEW">You choose (${manual.length})</option>
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
           <p>Nothing left to decide in this category.</p></div></div>`;
    };
    $('#rvFilter').addEventListener('change', render);
    render();
  } catch (e) { failed(e, 'Could not build the review list'); }
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
      <b>${h(m ? m.local_text : 'Mapping ' + r.mapping_id)}</b>
      ${context ? `<span class="chip">${h(context)}</span>` : ''}
      ${pill(r.decision)} ${statPill(r.terminology_status)}
    </div>
    <p class="hint">
      ${m ? `<span class="mono">${h(m.source_dataset)} / ${h(m.local_code)}</span> · ` : ''}
      currently mapped to
      <a href="#/lookup/${h(r.target_system)}/${encodeURIComponent(r.old_code)}" class="mono">${h(r.old_code)}</a>
      ${md.display ? '— ' + h(md.display) : ''}
      · checked against <span class="mono">${h(r.current_version || '')}</span></p>

    <p class="plain" style="background:var(--line-soft);padding:8px 10px;border-radius:6px">
      ${h(d.say)} ${r.reason && REASON[r.reason] ? h(REASON[r.reason]) : ''}</p>

    ${usable.length > 1 && context ? `<div class="note warn mt"><p><b>This one is on you.</b>
       Your test is run on <b>${h(context)}</b>. The options below usually differ by method or
       specimen — pick the one that matches how your lab actually performs it.</p></div>` : ''}

    ${tg.length ? `<div class="grid c2 mt">${tg.map(t => candCard(t, r.target_system)).join('')}</div>` : ''}

    ${usable.length ? `<div class="row mt">
        <div><label class="f">Change it to</label>
          <select id="sel-${r.id}">${opts}</select></div>
        <div style="flex:2 1 220px"><label class="f">Why <span class="faint">(optional, kept in the history)</span></label>
          <input type="text" id="rsn-${r.id}" placeholder="e.g. matches the method our lab uses"></div>
        <div class="auto"><button type="button" onclick="approve(${r.id}, ${r.mapping_id})">Approve</button></div>
      </div>`
    : `<div class="note warn mt mb0"><p><b>Nothing to pick from.</b> No usable replacement has
       been published for this code, so there is no shortcut here — someone has to work out the
       right code by hand. Once you know it, it can be applied through the API with
       <span class="mono">allow_unsuggested</span>, and it will still be checked against
       today's release before it is accepted.</p></div>`}
  </div>`;
}

window.approve = async (resultId, mappingId) => {
  const reviewer = ($('#rvName').value || '').trim();
  if (!reviewer) {
    toast('Please put your name in first — an approval has to belong to someone.', 'bad');
    $('#rvName').focus();
    return;
  }
  const target = $(`#sel-${resultId}`).value;
  const reason = ($(`#rsn-${resultId}`).value || '').trim();

  modal('Confirm this change', `
    <p>Here is exactly what will happen:</p>
    <dl class="kv">
      <dt>Mapping</dt><dd>#${mappingId}</dd>
      <dt>New code</dt><dd class="mono">${h(target)}</dd>
      <dt>Approved by</dt><dd>${h(reviewer)}</dd>
      <dt>Reason</dt><dd>${reason ? h(reason) : '<span class="faint">none given</span>'}</dd>
    </dl>
    <div class="note mt"><p>The old code and the release it was valid in are written to a new
      history row. Nothing is deleted. Before the change is accepted, the new code is checked
      once more against today's release — if it is not valid, the approval is refused.</p></div>
    <div class="row mt">
      <div class="auto"><button type="button" id="okBtn">Yes, approve it</button></div>
      <div class="auto"><button type="button" class="quiet" onclick="closeModal()">Cancel</button></div>
    </div>`);

  $('#okBtn').addEventListener('click', async () => {
    const btn = $('#okBtn'); btn.disabled = true; btn.innerHTML = 'Applying… <span class="spin"></span>';
    try {
      await api(`/api/v1/mappings/${mappingId}/approve-replacement`, {
        method: 'POST',
        body: JSON.stringify({
          target_code: target, reviewer, reason: reason || null, audit_result_id: resultId,
          allow_unsuggested: false,
        }),
      });
      closeModal();
      toast(`Mapping #${mappingId} now points at <span class="mono">${h(target)}</span>. `
          + `The old code is safe in the history.`, 'ok');
      const card = $(`#rc-${resultId}`);
      if (card) {
        card.style.opacity = '.55';
        card.insertAdjacentHTML('beforeend',
          `<div class="note ok mt mb0"><p>✔ Approved by <b>${h(reviewer)}</b>.
           <a href="#/mappings/${mappingId}">See the history</a></p></div>`);
        card.querySelectorAll('button, select, input').forEach(x => x.disabled = true);
      }
      refreshBadge();
    } catch (e) {
      closeModal();
      toast('Refused: ' + h(e.message), 'bad');
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
        <h1>Compare versions</h1>
        <p class="lede">Put two releases side by side and see what moved. For LOINC we also
        check our own answer against <b>the change log the publisher ships with the
        release</b> — so you do not have to take our word for it.</p></div>

      <div class="card">
        <div class="row">
          <div class="narrow"><label class="f" for="cSys">Terminology</label>
            <select id="cSys">${Object.keys(bySys).map(s =>
              `<option value="${h(s)}">${h(sysName(s))}</option>`).join('')}</select></div>
          <div><label class="f" for="cOld">Older version</label><select id="cOld"></select></div>
          <div><label class="f" for="cNew">Newer version</label><select id="cNew"></select></div>
          <div class="auto"><button id="cGo" type="button">Compare</button></div>
        </div>
        <p class="hint mt mb0">Comparing two full LOINC releases takes a few seconds — it means
          reading a couple of hundred thousand rows.</p>
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
      if (old === nw) { toast('Pick two different versions.', 'bad'); return; }
      $('#cOut').innerHTML = `<div class="empty"><span class="spin"></span>
        <p>Comparing ${h(old)} with ${h(nw)}…</p></div>`;
      try {
        const d = await api(`/api/v1/releases/diff?system=${encodeURIComponent(sys)}`
          + `&old=${encodeURIComponent(old)}&new=${encodeURIComponent(nw)}`);
        $('#cOut').innerHTML = diffCard(d);
      } catch (e) {
        $('#cOut').innerHTML = `<div class="note bad"><p>${h(e.message)}</p></div>`;
      }
    };

    $('#cGo').addEventListener('click', () => {
      const target = `#/compare/${$('#cSys').value}/${encodeURIComponent($('#cOld').value)}`
                   + `/${encodeURIComponent($('#cNew').value)}`;
      if (location.hash !== target) { location.hash = target; } else { run(); }
    });

    if (rest && rest.length >= 3) run();
  } catch (e) { failed(e, 'Could not load the version list'); }
};

function diffCard(d) {
  const v = d.validation || {};
  const isLoinc = d.system === 'LOINC';
  const missed = v.missed_changes ?? null;

  const validation = (isLoinc && v.change_snapshot_available !== false) ? `
    <div class="card">
      <h2>Our answer, checked against the publisher's own</h2>
      <p class="hint">LOINC ships a list of everything it changed. We work the changes out
        ourselves and then compare. The number that matters is <b>how many we missed</b>.</p>
      <div class="grid c4">
        <div class="stat"><div class="n plain">${n(v.official_changes)}</div>
          <div class="k">they published</div></div>
        <div class="stat"><div class="n plain">${n(v.detected_changes)}</div>
          <div class="k">we found</div></div>
        <div class="stat ${missed === 0 ? 'ok' : 'bad'}"><div class="n">${n(missed)}</div>
          <div class="k">we missed</div><div class="sub">must be 0</div></div>
        <div class="stat ${(v.unexpected_changes || 0) === 0 ? 'ok' : 'warn'}">
          <div class="n">${n(v.unexpected_changes)}</div>
          <div class="k">we over-reported</div><div class="sub">must be 0</div></div>
      </div>
      ${missed === 0
        ? `<div class="note ok mt mb0"><p>✔ Every one of the ${n(v.official_changes)} published
           changes was found — none missed, none invented. This is the strongest evidence the
           tool can give you about its own correctness.</p></div>`
        : `<div class="note bad mt mb0"><p>${plural(missed, 'published change')} went
           undetected. That is a bug, not a finding.</p></div>`}
      ${Object.keys(v.unsupported_official_properties || {}).length ? `
        <p class="hint mt mb0">Fields we do not track — counted and named here rather than
        quietly dropped:
        ${Object.entries(v.unsupported_official_properties).map(([k, c]) =>
          `<span class="chip">${h(k)} ${n(c)}</span>`).join(' ')}</p>` : ''}
    </div>` : '';

  const trans = d.status_transitions || {};
  const fields = d.changes_by_field || {};

  return `<div class="grid c4">
      <div class="stat"><div class="n plain">${n(d.old_total)}</div>
        <div class="k">codes in ${h(d.old_version)}</div></div>
      <div class="stat"><div class="n plain">${n(d.new_total)}</div>
        <div class="k">codes in ${h(d.new_version)}</div></div>
      <div class="stat ok"><div class="n">${n(d.new_codes)}</div><div class="k">newly added</div></div>
      <div class="stat ${(d.removed_codes || 0) === 0 ? 'ok' : 'bad'}">
        <div class="n">${n(d.removed_codes)}</div><div class="k">disappeared</div>
        <div class="sub">${(d.removed_codes || 0) === 0
          ? 'as it should be — codes are never deleted'
          : 'unexpected: worth investigating'}</div></div>
    </div>
    ${validation}
    <div class="grid c2">
      <div class="card mb0"><h2>Codes that changed status</h2>
        <p class="hint">It goes both ways — retired codes are sometimes brought back.</p>
        ${Object.keys(trans).length ? `<table class="tbl">
          <tbody>${Object.entries(trans).sort((a, b) => b[1] - a[1]).map(([k, c]) =>
            `<tr><td class="small">${h(prettyTransition(k))}</td><td class="num">${n(c)}</td></tr>`).join('')}
          </tbody></table>` : '<p class="muted">No status changed between these two.</p>'}
      </div>
      <div class="card mb0"><h2>Which fields changed</h2>
        <p class="hint">${n(d.total_field_changes)} edits across ${n(d.changed_codes)} codes.</p>
        ${Object.keys(fields).length ? `<table class="tbl">
          <tbody>${Object.entries(fields).sort((a, b) => b[1] - a[1]).map(([k, c]) =>
            `<tr><td class="small">${h(prettyField(k))}</td><td class="num">${n(c)}</td></tr>`).join('')}
          </tbody></table>` : '<p class="muted">Nothing changed.</p>'}
      </div>
    </div>`;
}

function prettyTransition(t) {
  const word = s => ({
    ACTIVE: 'active', TRIAL: 'trial', DISCOURAGED: 'discouraged',
    DEPRECATED: 'retired', INACTIVE: 'inactive',
  })[s.trim()] || s.trim().toLowerCase();
  const parts = t.split('->');
  return parts.length === 2 ? `${word(parts[0])} → ${word(parts[1])}` : t;
}

function prettyField(f) {
  return ({
    status: 'status', long_common_name: 'full name', short_name: 'short name',
    component: 'what is measured', property: 'kind of measurement',
    time_aspect: 'timing', system: 'specimen', scale_type: 'scale',
    method_type: 'method', class_name: 'class',
  })[f] || f.replace(/_/g, ' ');
}

// ================================================================== RELEASES
ROUTES.releases = async () => {
  loading();
  try {
    const rows = await api('/api/v1/releases');
    view().innerHTML = `<div class="page-head">
        <h1>Versions loaded</h1>
        <p class="lede">Everything ever imported stays here. A newer release does not replace
        an older one — only the "in use now" flag moves. That is what lets you re-run last
        year's check and get last year's answer.</p></div>
      <div class="card"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Terminology</th><th>Version</th><th>In force since</th><th>State</th>
          <th>Loaded</th><th>From file</th><th>Fingerprint</th><th>What came in</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td>${h(sysName(r.system))}</td>
          <td class="mono"><b>${h(r.version)}</b></td>
          <td class="small">${dash(r.effective_date)}</td>
          <td>${r.is_current ? '<span class="pill keep">in use now</span>'
                             : '<span class="pill neutral">superseded</span>'}</td>
          <td class="small">${when(r.imported_at)}</td>
          <td class="mono small">${h(r.source_filename)}</td>
          <td class="mono small faint" title="${h(r.sha256)}">${h(r.sha256.slice(0, 16))}…</td>
          <td class="small faint">${dash(r.notes)}</td>
        </tr>`).join('')}</tbody></table></div></div>

      <div class="note"><p><b>What is a fingerprint for?</b> It is computed from the file's
      contents. Rename the file and it stays the same; change one character inside and it
      changes completely. So the same release can never be imported twice by accident, whatever
      it happens to be called.</p></div>`;
  } catch (e) { failed(e, 'Could not load the version list'); }
};

// ====================================================================== HELP
ROUTES.help = () => {
  const rows = Object.entries(DECISION).map(([k, d]) => `<tr>
      <td>${pill(k)}</td><td class="mono small">${h(k)}</td><td>${h(d.say)}</td></tr>`).join('');
  const st = Object.entries(STATUS).map(([k, s]) => `<tr>
      <td>${statPill(k)}</td><td class="mono small">${h(k)}</td><td>${h(s.say)}</td></tr>`).join('');
  const rs = Object.entries(REASON).map(([k, v]) => `<tr>
      <td class="mono small nowrap">${h(k)}</td><td>${h(v)}</td></tr>`).join('');

  view().innerHTML = `<div class="page-head">
      <h1>What the words mean</h1>
      <p class="lede">Every technical word this app can show you, in plain English.</p></div>

    <div class="card"><h2>Start here: a worked example</h2>
      <p>Say your lab has a test called <b>INR(PT)</b> — the standard clotting test for anyone
      on warfarin. Years ago somebody mapped it to LOINC code <span class="mono">5895-7</span>.</p>
      <p>LOINC has since <b>retired</b> that code and published exactly one successor,
      <span class="mono">6301-6</span>. The old code was not deleted — it still resolves, still
      looks perfectly normal in your database, and nothing anywhere would have flagged it.</p>
      <p>So this app says: status <b>Retired</b>, verdict <b>Replacement available</b>, because
      <i>exactly one official replacement is published</i>. It shows you the new code and waits.
      It will not switch anything until you say so — and when you do, your name and the old code
      are kept for good.</p>
      <p class="mb0"><a href="#/lookup/LOINC/5895-7">See that exact case →</a></p></div>

    <div class="card"><h2>The five verdicts</h2>
      <p class="hint">The engine can only ever say one of these five things. That is deliberate:
        a fixed vocabulary is one you can audit.</p>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Verdict</th><th>In the API</th><th>What it means</th></tr></thead>
        <tbody>${rows}</tbody></table></div></div>

    <div class="card"><h2>What a code's status means</h2>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Status</th><th>In the API</th><th>What it means</th></tr></thead>
        <tbody>${st}</tbody></table></div></div>

    <div class="card"><h2>The reason codes</h2>
      <p class="hint">Every verdict comes with one of these, so you can always ask "why?"</p>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Reason</th><th>What it means</th></tr></thead>
        <tbody>${rs}</tbody></table></div></div>

    <div class="card"><h2>Three rules this app never breaks</h2>
      <ol>
        <li><b>When it is not sure, it says so.</b> If two replacements are equally official, it
          will not pick one for you. A wrong answer delivered confidently is worse than no answer
          at all.</li>
        <li><b>It never changes anything by itself.</b> A check only records what it found.
          Changing a code takes a named person pressing a button, and the new code is
          re-validated at that moment.</li>
        <li><b>It never forgets.</b> Change a code and the old one, the release it was valid in,
          who changed it and why are all kept. Six months from now you can still reconstruct
          exactly why a decision was made.</li>
      </ol></div>

    <div class="card"><h2>Why every answer names a version</h2>
      <p class="mb0">A code is not valid or invalid in the abstract — it is valid <i>in a
      particular release</i>. An answer that was right in 2026 can be wrong in 2027, and an
      answer that hides which release it came from cannot be checked, reproduced or defended.
      So this app will not show you a verdict without telling you what it was measured against.
      That single habit is the whole point of the project.</p></div>`;
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
    line.innerHTML = `<span class="pill ${ok ? 'keep' : 'warning'}">${ok ? 'Connected' : h(hh.status)}</span>`
      + `<div class="mt small">Database ${hh.database ? '✔' : '✘'}`
      + ` · Search server ${hh.snowstorm?.available
          ? '✔'
          : '<span title="Optional. Everything except free-text search works without it.">off</span>'}</div>`;
  } catch {
    line.innerHTML = `<span class="pill unknown">Not connected</span>
      <div class="mt small">Is the server still running?</div>`;
  }
}

$('#themeBtn').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
  if (next) {
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('vas.theme', next);
    toast(`Theme: ${next}`, '');
  } else {
    document.documentElement.removeAttribute('data-theme');
    localStorage.removeItem('vas.theme');
    toast('Theme: follows your system', '');
  }
});
const savedTheme = localStorage.getItem('vas.theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);


/* ==========================================================================
   Result standardization
   ==========================================================================
   The terminology pages answer "is this code still right?". These answer the
   question underneath it: "what did the test actually say, and can another
   system read it?"

   The presentation that carries the most meaning here is before-and-after on a
   single row -- the messy thing the hospital recorded, next to the clean thing
   it became. A table of percentages tells you the pipeline ran; one row shown
   both ways tells you what it did. */

const ISSUE_MEANING = {
  TEXT_RESULT: {
    tone: 'ok', short: 'a word, not a number',
    say: 'The result is a category like "Negative" or "Trace". It is kept as text — '
       + 'turning it into a number would change what it means.',
  },
  CODE_PENDING_LICENCE: {
    tone: 'ok', short: 'wording standardised, code pending',
    say: 'The wording was standardised, but no standard code was attached, because '
       + 'SNOMED CT International is not licensed here. Inventing one would be worse '
       + 'than the gap.',
  },
  BELOW_DETECTION_LIMIT: {
    tone: 'ok', short: 'below what the test can measure',
    say: 'A result like "<2.0". The number and the "<" are both kept — dropping the '
       + 'sign would turn a limit into a measurement.',
  },
  ABOVE_DETECTION_LIMIT: {
    tone: 'ok', short: 'above what the test can measure',
    say: 'A result like ">12000". The number and the ">" are both kept.',
  },
  NO_LOINC_MAPPING: {
    tone: 'warn', short: 'this test was never given a code',
    say: 'Nobody ever assigned a LOINC code to this test. The value, unit and time are '
       + 'still standardized and usable — it just cannot be compared with another '
       + 'system until somebody maps it.',
  },
  LOINC_NOT_APPROVED: {
    tone: 'warn', short: 'the code is no longer right',
    say: 'The code this test carries has been retired or discouraged. The result is '
       + 'still standardized, but no approved code is attached until a person decides.',
  },
  LOINC_UNKNOWN_CODE: {
    tone: 'bad', short: 'the code is not in LOINC',
    say: 'The code on this test does not exist in the current LOINC release at all.',
  },
  LOINC_TRIAL: {
    tone: 'warn', short: 'the code is provisional',
    say: 'The code is published but still marked TRIAL, so it may change.',
  },
  UNIT_MISSING: {
    tone: 'warn', short: 'no unit was recorded',
    say: 'The result has a number but no unit. Nothing is guessed — a unit taken from '
       + 'the LOINC example would often be wrong.',
  },
  UNIT_UNKNOWN: {
    tone: 'warn', short: 'we have no rule for this unit',
    say: 'The unit is not one we have a rule for, so the value and the original unit '
       + 'are kept exactly as they arrived and nothing is converted.',
  },
  UNIT_INCOMPATIBLE: {
    tone: 'bad', short: 'that unit cannot belong to this test',
    say: 'The unit measures a different kind of quantity than the test produces — a '
       + 'time on a concentration, say. The row is quarantined rather than corrected.',
  },
  UNIT_CONVERSION_NOT_AVAILABLE: {
    tone: 'warn', short: 'no approved conversion',
    say: 'Converting this unit would change the number, and that needs an approved, '
       + 'test-specific rule. Without one the original value is kept.',
  },
  SCALE_MISMATCH: {
    tone: 'warn', short: 'the code and the answer disagree',
    say: 'LOINC says this test produces one kind of answer and the lab reported '
       + 'another — a numeric code with a worded result, or the reverse. Usually it '
       + 'means the code chosen for the test is not quite the right one.',
  },
  VALUE_NUMERIC_MISMATCH: {
    tone: 'warn', short: 'the source disagrees with itself',
    say: 'The text and the numeric column of the source hold different numbers. We '
       + 'keep the text, because that is what a person wrote down, and flag it.',
  },
  MISSING_VALUE: {
    tone: 'warn', short: 'nothing was recorded',
    say: 'No result was recorded. It is stored as absent with a reason, never as zero.',
  },
  NOT_A_NUMBER: {
    tone: 'warn', short: 'the test did not produce a result',
    say: 'The row records something like "NotDone" or "HOLD" — what happened to the '
       + 'specimen, not a finding. Stored as absent rather than as a result.',
  },
  PARSE_ERROR: {
    tone: 'bad', short: 'the value could not be read',
    say: 'The value could not be read as anything sensible. It is kept verbatim.',
  },
  CATEGORICAL_UNMAPPED: {
    tone: 'warn', short: 'we do not know this wording',
    say: 'No rule exists for this text yet, so it is preserved exactly as written and '
       + 'nothing is assumed about what it means.',
  },
  UNKNOWN_ITEMID: {
    tone: 'bad', short: 'the test is not in the dictionary',
    say: 'There is no dictionary entry for this test, so there is no way to say what '
       + 'the result belongs to. The row is quarantined.',
  },
};

const VALUE_TYPE_MEANING = {
  QUANTITY: { tone: 'keep', label: 'a number', say: 'A measured quantity, with a unit.' },
  CODEABLE_CONCEPT: { tone: 'suggest', label: 'a category', say: 'A word like "Negative" or "1+".' },
  STRING: { tone: 'warning', label: 'free text', say: 'A sentence, kept as written.' },
  ABSENT: { tone: 'unknown', label: 'nothing recorded', say: 'No result — stored as absent, never as zero.' },
  UNDETERMINED: {
    tone: 'unknown', label: 'could not be read',
    say: 'Quarantined before the value could be read — usually a test with no dictionary entry.',
  },
};

const UNIT_STATUS_MEANING = {
  UNIT_VALID: 'already a standard UCUM unit',
  UNIT_NORMALIZED: 'spelling standardised; the number is unchanged',
  UNIT_CONVERTED: 'an approved rule changed the number too',
  UNIT_MISSING: 'no unit was recorded',
  UNIT_UNKNOWN: 'no rule for this unit yet',
  UNIT_INCOMPATIBLE: 'wrong kind of quantity for this test',
  UNIT_REVIEW_REQUIRED: 'needs a person to decide',
};

const QUALITY_MEANING = {
  OK: { tone: 'keep', label: 'clean', say: 'Nothing worth flagging.' },
  WARNING: { tone: 'warning', label: 'usable, with a note', say: 'Fine to use, but something is worth knowing.' },
  QUARANTINED: { tone: 'unknown', label: 'quarantined', say: 'Not fit to use as it stands. Kept, never deleted.' },
};

const issueInfo = c => ISSUE_MEANING[c] || { tone: 'warn', short: c, say: '' };
const issueTone = c => ({ ok: 'keep', warn: 'warning', bad: 'unknown' })[issueInfo(c).tone] || 'neutral';

// ------------------------------------------------- what happened to the data
ROUTES.results = async () => {
  loading('Reading the last standardization run…');
  try {
    const cov = await api('/api/v1/standardization/coverage');
    const t = cov.terminology || {};
    const u = cov.units || {};
    const vt = cov.by_value_type || {};
    const q = cov.quality || {};
    const total = cov.input_rows || 1;
    const share = v => `${((v / total) * 100).toFixed(2)}%`;

    // The funnel is the story: every row starts at the top, and each stage says
    // how many made it through and what happened to the rest.
    const funnel = [
      {
        label: 'Results came in', n: cov.input_rows, tone: 'ink',
        say: 'Every raw row from the hospital extract.',
      },
      {
        label: 'Kept, nothing lost', n: cov.input_rows, tone: 'ok',
        say: cov.rows_accounted_for
          ? 'Rows in equals rows out. Nothing was silently dropped — that is checked, not assumed.'
          : 'ROWS DO NOT ADD UP. This is a bug and should be investigated.',
      },
      {
        label: 'The test has a code', n: t.with_any_code, tone: 'ink',
        say: `${n(t.no_code_at_all)} results are for tests nobody ever assigned a code to.`,
      },
      {
        label: 'The code is still right', n: t.with_approved_code, tone: 'ok',
        say: `${n(t.present_but_stale)} results carry a code that exists but has been retired `
           + `or discouraged. That gap is the whole reason this project exists.`,
      },
      {
        label: 'Numbers got a standard unit', n: u.with_ucum, tone: 'ok',
        say: `Of the ${n(u.numeric_rows)} results that are numbers, `
           + `${((u.ucum_rate_of_numeric || 0) * 100).toFixed(1)}% ended with a UCUM unit `
           + `another system can read.`,
      },
    ];

    const issueRows = Object.entries(cov.issues || {})
      .sort((a, b) => b[1] - a[1])
      .map(([code, count]) => {
        const info = issueInfo(code);
        return `<tr>
          <td><span class="pill ${issueTone(code)}">${h(info.short)}</span></td>
          <td class="num">${n(count)}</td>
          <td class="num faint">${share(count)}</td>
          <td>${h(info.say)}</td>
          <td class="mono small faint">${h(code)}</td>
        </tr>`;
      }).join('');

    view().innerHTML = `<div class="page-head">
        <h1>What happened to the data</h1>
        <p class="lede">Every laboratory result from the source, turned into a standard form:
        the right code, a properly typed value, and a unit another system can read. Run
        #${cov.run_id}, judged against <b>LOINC ${h(cov.loinc_version || '—')}</b>.</p></div>

      <div class="card">
        <h2>The journey, in five steps</h2>
        <p class="hint">Each step shows how many results made it through, and what happened
          to the ones that did not.</p>
        ${funnel.map((f, i) => `
          <div style="display:flex;gap:14px;align-items:flex-start;padding:11px 0${
            i < funnel.length - 1 ? ';border-bottom:1px solid var(--line-soft)' : ''}">
            <div style="flex:0 0 132px;text-align:right">
              <div style="font-size:20px;font-weight:700;color:var(--${f.tone})">${n(f.n)}</div>
              <div class="faint small">${share(f.n)}</div>
            </div>
            <div style="flex:1 1 auto">
              <b>${h(f.label)}</b>
              <div class="muted small">${f.say}</div>
            </div>
          </div>`).join('')}
      </div>

      <div class="grid c2">
        <div class="card mb0">
          <h2>What kind of answer each result was</h2>
          <p class="hint">Not every laboratory result is a number, and the ones that are not
            must not be forced into one.</p>
          <table class="tbl"><tbody>
            ${Object.entries(VALUE_TYPE_MEANING).map(([k, v]) => `<tr>
              <td><span class="pill ${v.tone}">${h(v.label)}</span></td>
              <td class="num">${n(vt[k] || 0)}</td>
              <td class="num faint">${share(vt[k] || 0)}</td>
              <td class="small muted">${h(v.say)}</td>
            </tr>`).join('')}
          </tbody></table>
        </div>

        <div class="card mb0">
          <h2>How much can be trusted as it stands</h2>
          <p class="hint">A quarantined row is kept with its reason attached — it is never
            deleted, because a shorter table that looks fine is the worst outcome.</p>
          <table class="tbl"><tbody>
            ${Object.entries(QUALITY_MEANING).map(([k, v]) => `<tr>
              <td><span class="pill ${v.tone}">${h(v.label)}</span></td>
              <td class="num">${n(q[k] || 0)}</td>
              <td class="num faint">${share(q[k] || 0)}</td>
              <td class="small muted">${h(v.say)}</td>
            </tr>`).join('')}
          </tbody></table>
        </div>
      </div>

      <div class="card">
        <h2>Everything worth knowing about, and how often</h2>
        <p class="hint">One result can raise more than one of these, so the numbers add up to
          more than the total. Nothing here was discarded — every count is rows still in the
          database with their original value intact.</p>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>What it is</th><th class="num">Results</th><th class="num">Share</th>
            <th>What it means</th><th>Code</th></tr></thead>
          <tbody>${issueRows || '<tr><td colspan="5" class="empty">Nothing flagged.</td></tr>'}</tbody>
        </table></div>
      </div>

      <div class="row">
        <div class="auto"><a href="#/browse"><button type="button">Look at individual results</button></a></div>
        <div class="auto"><a href="#/unmapped"><button class="ghost" type="button">Tests with no code</button></a></div>
      </div>`;
  } catch (e) {
    if (e.status === 404) {
      view().innerHTML = `<div class="page-head"><h1>What happened to the data</h1></div>
        <div class="card"><div class="empty"><div class="big">🧾</div>
          <p>No laboratory results have been standardized yet.</p>
          <p class="small muted">Load them, then standardize them:</p>
          <pre style="text-align:left;max-width:640px;margin:12px auto">python scripts/import_mimic_labevents.py --file &lt;LABEVENTS source&gt;
python scripts/standardize_mimic_results.py --seed-rules</pre>
        </div></div>`;
      return;
    }
    failed(e, 'Could not load the standardization summary');
  }
};

// ------------------------------------------------------------ browse results
ROUTES.browse = async (rest) => {
  const state = { quality: '', valueType: '', search: '', offset: 0, limit: 25 };
  if (rest && rest[0]) state.search = decodeURIComponent(rest[0]);

  view().innerHTML = `<div class="page-head">
      <h1>Browse results</h1>
      <p class="lede">Each card shows one laboratory result twice: what the hospital
      recorded, and what it became. Seeing both is the only way to tell whether
      standardizing changed the meaning.</p></div>

    <div class="card">
      <div class="row">
        <div><label class="f" for="bSearch">Search</label>
          <input type="text" id="bSearch" placeholder="test name, LOINC code or itemid…"
            value="${h(state.search)}" autocomplete="off"></div>
        <div class="narrow"><label class="f" for="bQuality">Trust</label>
          <select id="bQuality"><option value="">All</option>
            <option value="OK">Clean</option>
            <option value="WARNING">With a note</option>
            <option value="QUARANTINED">Quarantined</option></select></div>
        <div class="narrow"><label class="f" for="bType">Answer</label>
          <select id="bType"><option value="">All</option>
            <option value="QUANTITY">A number</option>
            <option value="CODEABLE_CONCEPT">A category</option>
            <option value="STRING">Free text</option>
            <option value="ABSENT">Nothing recorded</option></select></div>
        <div class="auto"><button id="bGo" type="button">Show</button></div>
      </div>
      <p class="hint mt mb0" id="bCount"></p>
    </div>
    <div id="bList"></div>
    <div class="row" id="bPager" hidden>
      <div class="auto"><button class="quiet" type="button" id="bPrev">← Previous</button></div>
      <div class="auto"><button class="quiet" type="button" id="bNext">Next →</button></div>
    </div>`;

  const load = async () => {
    $('#bList').innerHTML = `<div class="empty"><span class="spin"></span></div>`;
    try {
      const params = new URLSearchParams({
        limit: String(state.limit), offset: String(state.offset),
      });
      if (state.quality) params.set('quality', state.quality);
      if (state.valueType) params.set('value_type', state.valueType);
      if (state.search) params.set('search', state.search);

      const runs = await api('/api/v1/standardization/runs?limit=1');
      if (!runs.length) throw Object.assign(new Error('no run'), { status: 404 });
      const data = await api(`/api/v1/standardization/runs/${runs[0].id}/results?${params}`);

      $('#bCount').innerHTML = data.total
        ? `${n(data.total)} results match. Showing ${n(data.offset + 1)}–${n(data.offset + data.returned)}.`
        : 'Nothing matches those filters.';
      $('#bList').innerHTML = data.results.length
        ? data.results.map(resultCard).join('')
        : `<div class="card"><div class="empty"><p>Nothing matches those filters.</p></div></div>`;
      $('#bPager').hidden = data.total <= state.limit;
      $('#bPrev').disabled = state.offset === 0;
      $('#bNext').disabled = state.offset + state.limit >= data.total;
    } catch (e) {
      if (e.status === 404) {
        $('#bList').innerHTML = `<div class="card"><div class="empty"><div class="big">🔬</div>
          <p>No results have been standardized yet.</p>
          <a href="#/results"><button type="button">See how to start</button></a></div></div>`;
        return;
      }
      $('#bList').innerHTML = `<div class="note bad"><p>${h(e.message)}</p></div>`;
    }
  };

  $('#bGo').addEventListener('click', () => {
    state.search = $('#bSearch').value.trim();
    state.quality = $('#bQuality').value;
    state.valueType = $('#bType').value;
    state.offset = 0;
    load();
  });
  $('#bSearch').addEventListener('keydown', e => { if (e.key === 'Enter') $('#bGo').click(); });
  ['#bQuality', '#bType'].forEach(s => $(s).addEventListener('change', () => $('#bGo').click()));
  $('#bPrev').addEventListener('click', () => {
    state.offset = Math.max(0, state.offset - state.limit); load();
  });
  $('#bNext').addEventListener('click', () => { state.offset += state.limit; load(); });

  load();
};

function resultCard(r) {
  const vt = VALUE_TYPE_MEANING[r.value_type] || { tone: 'neutral', label: r.value_type, say: '' };
  const quality = QUALITY_MEANING[r.quality_status] || { tone: 'neutral', label: r.quality_status };

  const before = `
    <div style="flex:1 1 280px">
      <div class="faint small" style="text-transform:uppercase;letter-spacing:.05em">
        What the hospital recorded</div>
      <dl class="kv mt">
        <dt>Test</dt><dd>${h(r.source_label || '—')}
          ${r.source_fluid ? `<span class="chip">${h(r.source_fluid)}</span>` : ''}</dd>
        <dt>Value</dt><dd class="mono">${r.raw_value === null || r.raw_value === ''
          ? '<span class="faint">(nothing)</span>' : h(r.raw_value)}</dd>
        <dt>Unit</dt><dd class="mono">${r.raw_unit ? h(r.raw_unit) : '<span class="faint">(none)</span>'}</dd>
        <dt>Code</dt><dd class="mono">${r.original_loinc_code
          ? h(r.original_loinc_code) : '<span class="faint">(never assigned)</span>'}</dd>
        <dt>Flag</dt><dd>${r.raw_flag ? h(r.raw_flag) : '<span class="faint">(none)</span>'}</dd>
      </dl>
    </div>`;

  let valueOut;
  if (r.value_type === 'QUANTITY') {
    valueOut = `<span class="mono" style="font-size:15px">${
      r.comparator ? `<b>${h(r.comparator)}</b> ` : ''}${h(r.standard_numeric_value)}</span>${
      r.standard_ucum_unit
        ? ` <span class="mono">${h(r.standard_ucum_unit)}</span> <span class="chip">UCUM</span>`
        : ' <span class="faint">no standard unit</span>'}`;
  } else if (r.value_type === 'ABSENT') {
    valueOut = `<span class="faint">nothing recorded</span>${
      r.data_absent_reason ? ` <span class="chip">${h(r.data_absent_reason)}</span>` : ''}`;
  } else {
    valueOut = `<span style="font-size:15px">${h(r.normalized_text_value || '—')}</span>${
      r.coded_value_code
        ? ` <span class="chip">${h(r.coded_value_code)}</span>`
        : ' <span class="chip" title="No SNOMED CT licence, so the wording is standardised but no code is attached.">text only</span>'}`;
  }

  const codeOut = r.approved_current_loinc
    ? `<a href="#/lookup/LOINC/${encodeURIComponent(r.approved_current_loinc)}" class="mono">${h(r.approved_current_loinc)}</a>
       <span class="pill keep">valid in ${h(r.current_loinc_version)}</span>`
    : r.engine_suggested_loinc
      ? `<span class="faint">none approved</span>
         <span class="pill suggest">${h(r.engine_suggested_loinc)} proposed</span>`
      : `<span class="faint">none</span>`;

  const after = `
    <div style="flex:1 1 280px">
      <div class="faint small" style="text-transform:uppercase;letter-spacing:.05em">
        What it became</div>
      <dl class="kv mt">
        <dt>Kind</dt><dd><span class="pill ${vt.tone}">${h(vt.label)}</span></dd>
        <dt>Value</dt><dd>${valueOut}</dd>
        <dt>Unit</dt><dd class="small muted">${r.unit_status
          ? h(UNIT_STATUS_MEANING[r.unit_status] || r.unit_status) : '—'}</dd>
        <dt>Code</dt><dd>${codeOut}</dd>
        <dt>Reading</dt><dd>${r.interpretation_code === 'A'
          ? '<span class="pill warning">flagged abnormal</span>'
          : '<span class="faint">not stated</span>'}</dd>
      </dl>
    </div>`;

  const issues = (r.issues || []).map(code => {
    const info = issueInfo(code);
    return `<div style="margin-top:6px">
      <span class="pill ${issueTone(code)}">${h(info.short)}</span>
      <span class="small muted"> ${h(info.say)}</span></div>`;
  }).join('');

  return `<div class="card">
    <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
      <b>${h(r.source_label || 'itemid ' + r.itemid)}</b>
      <span class="pill ${quality.tone}">${h(quality.label)}</span>
      <span class="chip mono">${h(r.charttime || '')}</span>
      <span class="chip mono faint" title="Pseudonymised patient key">${h((r.subject_key || '').slice(0, 8))}…</span>
    </div>
    <div style="display:flex;gap:26px;flex-wrap:wrap;padding-top:6px">
      ${before}
      <div style="flex:0 0 22px;align-self:center;font-size:20px;color:var(--accent)">→</div>
      ${after}
    </div>
    ${issues ? `<div style="margin-top:10px;border-top:1px solid var(--line-soft);padding-top:8px">${issues}</div>` : ''}
    <div class="mt">
      <button class="quiet sm" type="button" onclick="showFhir(${r.id})">Show as FHIR</button>
    </div>
  </div>`;
}

window.showFhir = async (id) => {
  modal('As a FHIR Observation', '<div class="empty"><span class="spin"></span></div>');
  try {
    const data = await api(`/api/v1/standardization/results/${id}/fhir`);
    const problems = data.validation_problems || [];
    modal('As a FHIR Observation', `
      <p class="hint">This is what another system would receive. The subject is a pseudonym;
        nothing here can be traced back to a patient without the key.</p>
      ${problems.length
        ? `<div class="note bad"><p>${problems.map(h).join('<br>')}</p></div>`
        : '<div class="note ok mb0"><p>✔ Valid against the R4 rules this exporter is responsible for.</p></div>'}
      <pre style="max-height:52vh;overflow:auto">${h(JSON.stringify(data.resource, null, 2))}</pre>`);
  } catch (e) {
    modal('As a FHIR Observation', `<div class="note bad"><p>${h(e.message)}</p></div>`);
  }
};

// -------------------------------------------------------- tests with no code
ROUTES.unmapped = async () => {
  loading('Finding the tests that were never coded…');
  try {
    const data = await api('/api/v1/standardization/unmapped?limit=300');
    view().innerHTML = `<div class="page-head">
        <h1>Tests with no code</h1>
        <p class="lede">${n(data.count)} tests in <span class="mono">${h(data.dataset)}</span>
        have never been given a LOINC code. There is nothing to re-check here — somebody has
        to choose a code, and the engine will not guess one.</p></div>

      <div class="note"><p>These are ordered by how much data rides on them, because that is
        what decides which are worth doing first. The units and example values are shown
        because a person choosing a code needs to see what the test actually produces, not
        just its name.</p></div>

      <div class="card"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>Test</th><th>Specimen</th><th class="num">Results</th>
          <th>Units seen</th><th>Example values</th></tr></thead>
        <tbody>${data.items.map(i => `<tr>
          <td>${h(i.label || '')}<div class="faint small mono">${h(i.itemid)}</div></td>
          <td class="small">${h([i.fluid, i.category].filter(Boolean).join(' · '))}</td>
          <td class="num">${n(i.result_count)}</td>
          <td class="small mono">${(i.observed_units || [])
            .map(([unit, c]) => `${h(unit)} <span class="faint">(${n(c)})</span>`)
            .join('<br>') || '<span class="faint">—</span>'}</td>
          <td class="small mono faint">${(i.examples || []).slice(0, 3)
            .map(e => h(String(e).slice(0, 18))).join(' · ')}</td>
        </tr>`).join('')}</tbody></table></div></div>`;
  } catch (e) {
    if (e.status === 404) {
      view().innerHTML = `<div class="page-head"><h1>Tests with no code</h1></div>
        <div class="card"><div class="empty"><p>No laboratory data has been loaded yet.</p></div></div>`;
      return;
    }
    failed(e, 'Could not load the unmapped tests');
  }
};

health();
refreshBadge();
go();
