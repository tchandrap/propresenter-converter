'use strict';
// Renderer: no Node access; everything goes through window.pp (preload.js).
const $ = (id) => document.getElementById(id);
const h = (tag, props, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
    else if (k === 'style') e.style.cssText = v;            // setAttribute('style') would be blocked by the CSP
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
};

// theme: follows the system until the user picks Light or Dark in Settings
const applyTheme = (t) => { document.documentElement.dataset.theme = t === 'dark' ? 'dark' : 'light'; };
applyTheme(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

const LIB = '';                           // source id for ProPresenter's own library
const S = {
  cfg: { mp4: true, pdf: false, outdir: '', recent: [], pictureSeconds: 5, overrideAll: false, allSeconds: 10, noVideoHold: true },
  source: LIB,                            // '' or a .proplaylist path
  trees: {},                              // source -> { tree, label, error }
  playlist: null,                         // the selected playlist node
  selected: new Set(),                    // item indexes ticked
  focus: null,                            // item previewed
  phCache: {},
  busy: false,
  previewSeq: 0,
  slideSettings: {},                      // 'source|playlist|item' -> { slide number: { seconds, poster } }
};

const setKey = (idx) => `${S.source}|${S.playlist ? S.playlist.uuid : ''}|${idx}`;
const settingsFor = (idx) => S.slideSettings[setKey(idx)] || {};
const baseName = (p) => p.split('/').pop();
// ---- ProPresenter-style controls: a field that shows "24s" next to a slider (the field accepts typing, the slider drags)
const fmtS = (v) => `${Math.round(v * 10) / 10}s`;
const ICONS = {
  clock: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="13" r="8"/><path d="M12 9v4.5l3 1.5M9.5 3h5"/></svg>',
  lock: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
  photo: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.6"/><path d="M4 17l5-4.5 4 3 3-2.5 4 3.5"/></svg>',
};
const icon = (name) => { const e = h('span', { class: 'ico' }); e.innerHTML = ICONS[name]; return e; };
function timeControl({ value, min, max, step = 0.5, hard = max, total = null, title = '', ico = null, icoEl = null, commit }) {
  const field = h('input', { class: 'pp-field', type: 'text', inputmode: 'decimal', value: fmtS(value), title });
  const rng = h('input', { class: 'pp-range', type: 'range', min, max, step, value: Math.min(max, value), title });
  const paint = () => rng.style.setProperty('--p', (max > min ? ((rng.value - min) / (max - min)) * 100 : 0) + '%');
  paint();
  let cur = value;
  field.onfocus = () => field.select();
  field.onkeydown = (e) => { if (e.key === 'Enter') field.blur(); };
  field.onchange = () => {
    const v = parseFloat(String(field.value).replace(/s\s*$/i, '').replace(',', '.'));
    if (!Number.isFinite(v)) { field.value = fmtS(cur); return; }
    cur = Math.max(min, Math.min(hard, v)); field.value = fmtS(cur); rng.value = Math.min(max, cur); paint(); commit(cur);
  };
  rng.oninput = () => { cur = Number(rng.value); field.value = fmtS(cur); paint(); };
  rng.onchange = () => commit(Number(rng.value));
  const wrap = h('span', { class: 'pp-time' }, icoEl || (ico ? icon(ico) : null), field, rng, total != null ? h('span', { class: 'pp-total', text: fmtS(total), title: 'Length of the video' }) : null);
  wrap.set = (v) => { if (document.activeElement === field) return; cur = v; field.value = fmtS(v); rng.value = Math.min(max, v); paint(); };
  wrap.field = field;
  return wrap;
}
const timing = () => ({ pictureSeconds: 5, overrideAll: !!S.cfg.overrideAll, allSeconds: S.cfg.allSeconds, noVideoHold: !!S.cfg.noVideoHold });
const formatValue = () => (S.cfg.mp4 && S.cfg.pdf ? 'both' : S.cfg.pdf ? 'pdf' : 'mp4');
const flatPlaylists = (nodes, path = []) => nodes.flatMap((n) => n.type === 'folder' ? flatPlaylists(n.children, [...path, n.name]) : [{ ...n, folderPath: path }]);

// ------------------------------------------------------------------ sources and tree
function renderSources() {
  const box = $('sources'); box.replaceChildren();
  const add = (id, label, removable) => box.append(h('div', { class: 'src' + (S.source === id ? ' on' : ''), title: id || 'ProPresenter library', onclick: () => selectSource(id) },
    h('span', { class: 'nm', text: label }),
    removable ? h('button', { class: 'x', title: 'Remove from list', onclick: (e) => { e.stopPropagation(); removeRecent(id); }, text: '×' }) : null));
  add(LIB, 'ProPresenter library', false);
  for (const p of S.cfg.recent || []) add(p, baseName(p), true);
}

async function removeRecent(p) {
  S.cfg = await window.pp.cfgSet({ recent: (S.cfg.recent || []).filter((x) => x !== p) });
  delete S.trees[p]; delete S.phCache[p];
  if (S.source === p) await selectSource(LIB); else renderSources();
}

async function loadTree(src, force) {
  if (!force && S.trees[src]) return S.trees[src];
  const r = await window.pp.tree(src || null);
  S.trees[src] = r.error ? { error: r.error, tree: [] } : { tree: r.tree, label: r.source };
  delete S.phCache[src];
  return S.trees[src];
}

async function selectSource(src, keepPlaylist) {
  if (document.body.classList.contains('importing')) showTab('playlist');
  S.source = src; renderSources();
  $('tree').replaceChildren(h('div', { class: 'empty', text: 'Reading…' }));
  await loadTree(src);
  if (!keepPlaylist) { S.playlist = null; S.selected.clear(); S.focus = null; }
  renderTree(); renderItems(); renderPreviewEmpty(); updateMake(); loadPlaceholders();
}

function renderTree() {
  const box = $('tree'); box.replaceChildren();
  const t = S.trees[S.source];
  if (!t) return;
  if (t.error) { box.append(h('div', { class: 'note err' }, t.error, ' ', h('button', { class: 'link', text: 'Open Settings', onclick: () => showTab('settings') }))); return; }
  const q = $('filter').value.trim().toLowerCase();
  const draw = (nodes, depth) => {
    for (const n of nodes) {
      if (n.type === 'folder') {
        const inner = flatPlaylists(n.children).filter((p) => !q || p.name.toLowerCase().includes(q));
        if (q && !inner.length) continue;
        box.append(h('div', { class: 'folder', style: `padding-left:${6 + depth * 12}px`, text: n.name }));
        draw(n.children, depth + 1);
      } else {
        if (q && !n.name.toLowerCase().includes(q)) continue;
        const real = n.items.filter((i) => i.kind !== 'header').length;
        box.append(h('div', { class: 'pl' + (S.playlist && S.playlist.uuid === n.uuid ? ' on' : ''), style: `padding-left:${8 + depth * 12}px`, onclick: () => selectPlaylist(n) },
          h('span', { class: 'nm', text: n.name }), h('span', { class: 'n', text: String(real) })));
      }
    }
  };
  draw(t.tree, 0);
  if (!box.children.length) box.append(h('div', { class: 'empty', text: q ? 'No playlist matches.' : 'No playlists here.' }));
}

function selectPlaylist(n) {
  S.playlist = n; S.selected.clear(); S.focus = null;
  renderTree(); renderItems(); renderPreviewEmpty(); updateMake();
}

// ------------------------------------------------------------------ items
function itemTag(it) {
  if (it.kind === 'placeholder') return it.status === 'empty' ? h('span', { class: 'tag empty', text: 'placeholder: empty' })
    : h('span', { class: 'tag ph', text: 'placeholder' });
  if (it.kind === 'media') return h('span', { class: 'tag media', text: 'media' });
  if (it.status === 'missing') return h('span', { class: 'tag miss', text: 'file missing' });
  return null;
}
const usable = (it) => it.kind !== 'header' && it.kind !== 'unknown' && it.status !== 'empty' && it.status !== 'missing';

function renderItems() {
  const box = $('items'); box.replaceChildren();
  const p = S.playlist;
  $('pl-title').textContent = p ? p.name : 'Pick a playlist';
  if (!p) { box.append(h('div', { class: 'empty', text: 'Pick a playlist on the left.' })); return; }
  for (const it of p.items) {
    if (it.kind === 'header') { box.append(h('div', { class: 'item header' }, h('span', { class: 'nm', text: it.name }))); continue; }
    const ok = usable(it);
    const cb = h('input', { type: 'checkbox', disabled: ok ? false : true, onclick: (e) => e.stopPropagation(), onchange: (e) => {
      if (e.target.checked) S.selected.add(it.index); else S.selected.delete(it.index); updateMake(); } });
    cb.checked = S.selected.has(it.index);
    box.append(h('div', { class: 'item' + (S.focus === it.index ? ' on' : ''), 'data-i': it.index, onclick: () => focusItem(it) },
      cb, h('span', { class: 'no', text: String(it.index) }), h('span', { class: 'nm', text: it.name, title: it.name }), itemTag(it)));
  }
}

$('sel-all').onclick = () => { if (!S.playlist) return; S.playlist.items.filter(usable).forEach((i) => S.selected.add(i.index)); renderItems(); updateMake(); };
$('sel-none').onclick = () => { S.selected.clear(); renderItems(); updateMake(); };

// ------------------------------------------------------------------ preview (what ProPresenter would show)
function renderPreviewEmpty() {
  $('pv-title').textContent = 'Preview'; $('pv-meta').textContent = '';
  $('pv').replaceChildren(h('div', { class: 'empty', text: 'Click an item to see its slides the way ProPresenter shows them.' }));
}

let changeTimer = null;
async function focusItem(it, quiet) {
  S.focus = it.index; renderItems();
  const pv = $('pv'); $('pv-title').textContent = it.name; $('pv-meta').textContent = '';
  if (it.kind === 'placeholder' && it.status === 'empty') {
    pv.replaceChildren(h('div', { class: 'note warn' }, h('b', { text: 'This placeholder is empty. ' }),
      'No presentation is linked to it yet. In ProPresenter drag the presentation from the Library onto this placeholder in the playlist, then click Refresh here.'));
    return;
  }
  if (it.status === 'missing') {
    pv.replaceChildren(h('div', { class: 'note err', text: 'The presentation file this item points to was not found. If this playlist came from another computer, open it as a .proplaylist file instead (the files are inside it).' }));
    return;
  }
  const scroll = pv.scrollTop;
  if (quiet) pv.classList.add('busy'); else pv.replaceChildren(h('div', { class: 'empty', text: 'Building preview… (videos and many slides can take a few seconds)' }));
  const seq = ++S.previewSeq;
  const r = await window.pp.preview({ proplaylist: S.source || null, playlist: S.playlist.uuid, index: it.index, timing: timing(), settings: settingsFor(it.index) });
  if (seq !== S.previewSeq) return;
  pv.classList.remove('busy');
  if (r.error) { pv.replaceChildren(h('div', { class: 'note err', text: r.error })); return; }
  S.pv = { it, r };
  drawPreview(it, r, scroll);
}

// Seconds boxes sit in each slide's label bar. The PDF picture control (slider + seconds) shows only when PDF is ticked.
function drawPreview(it, r, scroll) {
  const pv = $('pv');
  const total = r.total_seconds;
  const mmss = `${Math.floor(total / 60)}:${String(Math.round(total % 60)).padStart(2, '0')}`;
  $('pv-meta').textContent = S.cfg.mp4 ? `${r.slides.length} slides · ${mmss} · ${r.subtitle}` : `${r.slides.length} slides · ${r.subtitle}`;
  $('pv-total').textContent = `Total ${mmss} · ${Math.round(total)} s`;
  const standin = r.slides.some((s) => /stand-in/.test(s.desc));
  const grid = h('div', { class: 'pv-grid' });
  const cur = settingsFor(it.index);
  const change = (n, key, val) => {
    const all = { ...S.slideSettings }; const k = setKey(it.index);
    all[k] = { ...(all[k] || {}), [n]: { ...((all[k] || {})[n] || {}), [key]: val } };
    S.slideSettings = all;
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => focusItem(it, true), 700);
  };
  for (const s of r.slides) {
    const col = s.color || '#3a3a40';
    const rgb = [1, 3, 5].map((i) => parseInt(col.slice(i, i + 2), 16));
    const fg = rgb.reduce((a, b) => a + b, 0) > 450 ? '#000' : '#fff';
    const isVid = (s.kind === 'video' || s.kind === 'slidevideo') && s.vlen > 0;
    const ctl = [];
    if (S.cfg.mp4 && s.kind !== 'audio') {            // seconds on screen: MP4 only
      const own = !!(cur[s.n] && cur[s.n].seconds);
      let lockBtn = null;
      if (own) {
        lockBtn = h('button', { class: 'pp-lock', type: 'button', title: 'This slide has its own time. Click to go back to the shared time.' });
        lockBtn.innerHTML = ICONS.lock;
        lockBtn.onclick = () => {
          const all = { ...S.slideSettings }; const k = setKey(it.index); const one = { ...((all[k] || {})[s.n] || {}) };
          delete one.seconds;
          all[k] = { ...(all[k] || {}) }; if (Object.keys(one).length) all[k][s.n] = one; else delete all[k][s.n];
          if (!Object.keys(all[k]).length) delete all[k];
          S.slideSettings = all; focusItem(it, true);
        };
      }
      ctl.push(h('div', { class: 'c' }, timeControl({ ico: 'clock', icoEl: lockBtn, value: s.seconds, min: 0.5, max: isVid ? Math.max(1, s.vlen) : 60, step: 0.5, hard: 3600, total: isVid ? s.vlen : null,
        title: 'How long this slide stays on screen in the MP4. A video shorter than this holds its last frame; longer is cut. Type a number or drag.', commit: (v) => change(s.n, 'seconds', v) })));
    }
    if (S.cfg.pdf && isVid) {                           // which moment of the video becomes the PDF page
      const max = Math.max(0, Math.floor((s.vlen - 0.1) * 10) / 10), val = s.poster == null ? 0 : Math.min(max, s.poster);
      ctl.push(h('div', { class: 'c' }, timeControl({ ico: 'photo', value: val, min: 0, max, step: 0.1, hard: max, total: s.vlen,
        title: 'Which moment of the video is the picture in the PDF. Type a number or drag.', commit: (v) => change(s.n, 'poster', v) })));
    }
    grid.append(h('div', { class: 'slide' },
      h('img', { src: 'file://' + encodeURI(s.file).replace(/#/g, '%23'), alt: s.name, loading: 'lazy' }),
      h('div', { class: 'lab', style: `background:${col};color:${fg}` }, h('b', { text: String(s.n) }), h('span', { class: 't', text: s.name })),
      ctl.length ? h('div', { class: 'ctl pp' }, ...ctl) : null));
  }
  const notes = [];
  if (standin) notes.push(h('div', { class: 'note warn', text: 'ProPresenter themes are not stored in the files, so slides that only have text are shown on a dark stand-in background. The MP4 / PDF use the same stand-in.' }));
  if (Object.keys(cur).length) notes.push(h('div', { class: 'note' }, 'Custom times set on this item. ', h('button', { class: 'link', text: 'Reset to ProPresenter times', onclick: () => { const all = { ...S.slideSettings }; delete all[setKey(it.index)]; S.slideSettings = all; focusItem(it, true); } })));
  pv.replaceChildren(...notes, grid);
  pv.scrollTop = scroll || 0;
}
const redrawPreview = () => { if (S.pv && S.pv.it.index === S.focus) drawPreview(S.pv.it, S.pv.r, $('pv').scrollTop); };

// ------------------------------------------------------------------ placeholders tab
async function loadPlaceholders(force) {
  const src = S.source;
  if (force || !S.phCache[src]) {
    const r = await window.pp.placeholders(src || null);
    S.phCache[src] = r;
  }
  if (src !== S.source) return;
  const r = S.phCache[src];
  $('ph-count').textContent = r.error ? '!' : String(r.placeholders.length);
  $('ph-src').textContent = r.error ? '' : `Source: ${r.source}`;
  renderPlaceholders();
}

function renderPlaceholders() {
  const box = $('ph-table'); box.replaceChildren();
  const r = S.phCache[S.source];
  if (!r) return;
  if (r.error) { box.append(h('div', { class: 'note err', text: r.error })); return; }
  const q = $('ph-filter').value.trim().toLowerCase(), onlyEmpty = $('ph-empty').checked;
  const rows = r.placeholders.filter((p) => (!q || p.name.toLowerCase().includes(q) || p.playlist.toLowerCase().includes(q)) && (!onlyEmpty || p.status === 'empty'));
  box.append(h('div', { class: 'phr h' }, h('span', { text: 'Placeholder' }), h('span', { text: 'Belongs to playlist' }), h('span', { text: 'Item' }), h('span', { text: 'Linked presentation' }), h('span', {})));
  if (!rows.length) box.append(h('div', { class: 'empty', text: r.placeholders.length ? 'No placeholder matches.' : 'This source has no placeholders. See “Not in the list?” to add one.' }));
  for (const p of rows) {
    const status = p.status === 'empty' ? h('span', { class: 'bad c', text: 'EMPTY: drag a presentation onto it' })
      : p.status === 'missing' ? h('span', { class: 'warn c', text: (p.linked || '') + ' (file missing)' }) : h('span', { class: 'ok c', text: p.linked || '' });
    box.append(h('div', { class: 'phr' },
      h('span', { class: 'c', text: p.name, title: p.name }), h('span', { class: 'c', text: p.playlist, title: p.playlist }),
      h('span', { text: '#' + p.index }), status,
      h('button', { class: 'btn', text: 'Open', onclick: () => openPlaceholder(p) })));
  }
}
$('ph-filter').oninput = renderPlaceholders; $('ph-empty').onchange = renderPlaceholders;

async function openPlaceholder(p) {
  const t = await loadTree(S.source);
  const pl = flatPlaylists(t.tree).find((x) => x.uuid === p.playlist_uuid);
  if (!pl) return;
  showTab('playlist');
  selectPlaylist(pl);
  const it = pl.items.find((i) => i.index === p.index);
  if (it) { await focusItem(it); const el = document.querySelector(`.item[data-i="${it.index}"]`); if (el) el.scrollIntoView({ block: 'center' }); }
}

// ------------------------------------------------------------------ help
function renderHelp() {
  $('help').replaceChildren(
    h('h3', { text: 'A playlist is missing from the list' }),
    h('p', { text: 'The list shows what ProPresenter has saved in its own library. If you were sent a new .proplaylist file (for example from another computer) you have two ways to use it:' }),
    h('ol', {},
      h('li', {}, h('b', { text: 'Open it here (nothing is imported): ' }), 'click “Open .proplaylist file…” on the left and pick the file. It appears under Sources, and its presentations and media are read from inside the file.'),
      h('li', {}, h('b', { text: 'Or import it into ProPresenter: ' }), 'double-click the .proplaylist file (or drag it onto the Playlists sidebar in ProPresenter), let ProPresenter finish, then click Refresh here. It then shows up under “ProPresenter library”.')),
    h('h3', { text: 'A placeholder is missing, or shows EMPTY' }),
    h('p', { text: 'A placeholder is a reserved spot in a playlist for something that is filled in later (for example the weekly announcements). It only appears here once it exists in a playlist that ProPresenter has saved.' }),
    h('ol', {},
      h('li', { text: 'In ProPresenter, open the playlist and add a placeholder to it (right-click inside the playlist and choose the option to add a placeholder; wording can differ slightly between ProPresenter versions), then give it a name.' }),
      h('li', { text: 'Drag the presentation from the Library onto that placeholder so it is linked. A placeholder with nothing linked shows as EMPTY here and cannot be converted.' }),
      h('li', { text: 'Save in ProPresenter (it saves on its own after a moment) and click Refresh here.' })),
    h('h3', { text: 'The theme background is missing in text slides' }),
    h('p', { text: 'ProPresenter keeps themes outside the presentation files. Text-only slides are therefore drawn on a dark stand-in background.' }),
    h('h3', { text: 'Safety' }),
    h('p', {}, 'This app only ', h('b', { text: 'reads' }), ' ProPresenter’s files. It never changes anything in the ProPresenter folder. Videos from a .proplaylist are unpacked into a cache folder (', h('code', { text: '~/Library/Caches/pp2mp4' }), ') that you can delete at any time.'));
}

// ------------------------------------------------------------------ tabs, bottom bar
function showTab(name) {
  if (name === 'settings') renderSettings();
  if (name === 'import') { impFillTemplates(); impSync(); }
  $('src-import').classList.toggle('on', name === 'import'); document.body.classList.toggle('importing', name === 'import');
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.id === 'view-' + name));
}
document.querySelectorAll('.tab').forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
$('src-import').onclick = () => showTab('import');
$('src-import').onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showTab('import'); } };

// ------------------------------------------------------------------ import: playlist from a worship script and decks
const IMP = { pdf: '', khotbah: '', master: '', template: '', lines: 2, busy: false, last: null };
function impSync() {
  for (const k of ['pdf', 'khotbah', 'master']) {
    $('imp-' + k).textContent = IMP[k] ? baseName(IMP[k]) : 'none'; $('imp-' + k).title = IMP[k];
    $('imp-' + k + '-x').hidden = !IMP[k];
  }
  const any = !!(IMP.pdf || IMP.khotbah || IMP.master);
  $('imp-make').disabled = IMP.busy || !any;
  $('imp-make-wrap').classList.toggle('blocked', !IMP.busy && !any);
  $('imp-tip').replaceChildren(h('b', { text: 'Not ready yet' }), h('div', { text: 'Add the worship script or a PowerPoint deck.' }));
  $('imp-busy').hidden = !IMP.busy; $('imp-cancel').hidden = !IMP.busy;
  for (const id of ['imp-pdf-pick', 'imp-khotbah-pick', 'imp-master-pick', 'imp-template', 'imp-lines']) $(id).disabled = IMP.busy;
}
function impFillTemplates() {
  const sel = $('imp-template'); sel.replaceChildren();
  sel.append(h('option', { value: '', text: 'Automatic: the newest template playlist in ProPresenter' }));
  const t = S.trees[LIB];
  for (const p of (t && t.tree ? flatPlaylists(t.tree) : [])) if (p.items && p.items.length) sel.append(h('option', { value: 'lib:' + p.name, text: 'ProPresenter: ' + p.name }));
  if (IMP.template && IMP.template.startsWith('file:')) sel.append(h('option', { value: IMP.template, text: 'File: ' + baseName(IMP.template.slice(5)) }));
  sel.append(h('option', { value: '__file', text: 'Choose a finished .proplaylist file…' }));
  sel.value = [...sel.options].some((o) => o.value === IMP.template) ? IMP.template : '';
}
for (const k of ['pdf', 'khotbah', 'master']) {
  $('imp-' + k + '-pick').onclick = async () => { const f = await window.pp.pickInput(k === 'pdf' ? 'pdf' : 'pptx'); if (f) { IMP[k] = f; impSync(); } };
  $('imp-' + k + '-x').onclick = () => { IMP[k] = ''; impSync(); };
}
$('imp-template').onchange = async () => {
  const v = $('imp-template').value;
  if (v === '__file') {
    const f = await window.pp.pickInput('template');
    IMP.template = f ? 'file:' + f : IMP.template;
  } else IMP.template = v;
  S.cfg = await window.pp.cfgSet({ importTemplate: IMP.template }); impFillTemplates();
};
$('imp-lines').onchange = async () => { const v = Math.max(1, Math.min(8, Math.round(Number($('imp-lines').value) || 2))); $('imp-lines').value = v; IMP.lines = v; S.cfg = await window.pp.cfgSet({ importLines: v }); };
const impTemplateArg = () => (IMP.template.startsWith('file:') ? IMP.template.slice(5) : IMP.template.startsWith('lib:') ? IMP.template.slice(4) : '');
function impShow(res) {
  const box = $('imp-result'); box.hidden = false; box.className = 'imp-res'; box.replaceChildren();
  if (res.error) { box.append(h('div', { class: 'note err', text: res.error })); return; }
  box.append(h('h3', { text: `${res.playlist}: ${res.items} items` }));
  for (const r of res.report) box.append(h('div', { class: 'imp-row' }, h('span', { class: 'chip ' + r.status, text: r.status === 'generated' ? 'made' : r.status === 'partial' ? 'partly' : r.status }), h('span', { class: 'nm', text: r.item }), h('span', { class: 'muted', text: r.note || '' })));
  if (res.notes && res.notes.length) box.append(h('div', { class: 'note warn imp-notes' }, h('b', { text: 'Still to do by hand' }), ...res.notes.map((n) => h('div', { text: '• ' + n }))));
  box.append(h('div', { class: 'note imp-notes', text: 'Everything not listed above is copied unchanged from the template playlist. Motion backgrounds for songs are picked at random from the template\'s own motions: swap them in ProPresenter if you like.' }));
  const act = h('div', { class: 'imp-actions' });
  const save = h('button', { class: 'btn primary', text: res.saved ? 'Save another copy…' : 'Save…' });
  save.onclick = async () => { const r = await window.pp.importSave({ tmp: res.tmp, name: res.playlist }); if (r && r.path) { res.saved = r.path; impShow(res); } else if (r && r.error) box.append(h('div', { class: 'note err', text: r.error })); };
  act.append(save);
  if (res.saved) {
    act.append(h('span', { class: 'ok', text: '✓ Saved: ' + baseName(res.saved) }),
      h('button', { class: 'btn', text: 'Show in Finder', onclick: () => window.pp.reveal(res.saved) }),
      h('button', { class: 'btn', text: 'Review it here', onclick: async () => {
        const recent = [res.saved, ...(S.cfg.recent || []).filter((x) => x !== res.saved)].slice(0, 12);
        S.cfg = await window.pp.cfgSet({ recent }); delete S.trees[res.saved]; showTab('playlist'); await selectSource(res.saved);
      } }));
  }
  box.append(act);
}
$('imp-make').onclick = async () => {
  IMP.busy = true; impSync(); $('imp-result').hidden = true;
  const res = await window.pp.importRun({ pdf: IMP.pdf, khotbah: IMP.khotbah, master: IMP.master, template: impTemplateArg(), lines: IMP.lines });
  IMP.busy = false; impSync();
  if (res.error) { impShow(res); return; }
  const saved = await window.pp.importSave({ tmp: res.tmp, name: res.playlist });
  if (saved && saved.path) res.saved = saved.path;
  IMP.last = res; impShow(res);
};
$('imp-cancel').onclick = () => window.pp.cancel();

// ------------------------------------------------------------------ settings: theme and the ProPresenter folder
function renderSettings() {
  const cur = document.documentElement.dataset.theme;
  for (const t of ['light', 'dark']) { $('theme-' + t).classList.toggle('on', cur === t); $('theme-' + t).setAttribute('aria-pressed', cur === t ? 'true' : 'false'); }
  window.pp.ppInfo().then((i) => {
    $('pp-path').textContent = i.root; $('pp-path').title = i.root;
    $('pp-reset').disabled = !i.custom;
    const st = $('pp-status');
    st.className = 'note-line ' + (i.found ? 'ok' : 'warn');
    st.textContent = i.found
      ? (i.custom ? '✓ Found. Using the folder you chose.' : '✓ Found. Using ProPresenter’s standard folder.')
      : (i.custom ? 'No ProPresenter files found in this folder. Choose another folder or use the default.' : 'ProPresenter’s standard folder was not found on this computer. If ProPresenter keeps its files elsewhere, choose that folder; otherwise open a .proplaylist file.');
  });
}
async function setPpRoot(root) {
  S.cfg = await window.pp.cfgSet({ ppRoot: root });
  delete S.trees[LIB]; delete S.phCache[LIB];
  if (S.source === LIB) { S.playlist = null; S.selected.clear(); S.focus = null; await selectSource(LIB); }
  renderSettings();
}
for (const t of ['light', 'dark']) {
  $('theme-' + t).onclick = async () => { applyTheme(t); S.cfg = await window.pp.cfgSet({ theme: t }); renderSettings(); };
}
$('pp-pick').onclick = async () => {
  const r = await window.pp.ppPick();
  if (!r) return;
  if (r.error) { const st = $('pp-status'); st.className = 'note-line bad'; st.textContent = r.error; return; }
  await setPpRoot(r.root);
};
$('pp-reset').onclick = () => setPpRoot('');

function buildOpts() {
  const box = $('pv-opts');
  const check = (id, text, title) => h('label', { class: 'pp-check', title }, h('input', { id, type: 'checkbox' }), h('span', { class: 'box', text: '✓' }), text);
  const row = (label, ...kids) => h('div', { class: 'pp-row' }, h('span', { class: 'k', text: label + ':' }), h('div', { class: 'v' }, ...kids));
  const all = timeControl({ value: S.cfg.allSeconds, min: 0.5, max: 120, step: 0.5, hard: 3600, title: 'The time used for every slide when the box is ticked.', commit: (v) => timingChanged({ allSeconds: v }) });
  box.replaceChildren(
    h('div', { class: 'pp-top' }, h('span', { class: 'pp-h', text: 'MP4 timing' }), h('span', { class: 'tot', id: 'pv-total' })),
    row('Every slide', check('all-on', 'Same time for every slide', "Show every slide for the same time, replacing ProPresenter's timers and video lengths. A slide whose time you change by hand keeps its own time (shown with a lock); click the lock to release it."), all),
    row('After a video', check('no-vhold', 'Go straight to the next slide', 'After a video ends, do not hold its last frame')));
  $('all-on').onchange = () => timingChanged({ overrideAll: $('all-on').checked });
  $('no-vhold').onchange = () => timingChanged({ noVideoHold: $('no-vhold').checked });
  return { all };
}
const O = buildOpts();

function updateMake() {
  const n = S.selected.size;
  const btn = $('make');
  const anyFmt = S.cfg.mp4 || S.cfg.pdf;
  btn.textContent = n === 0 ? 'Export…' : n === 1 ? 'Export 1 item…' : `Export ${n} items…`;
  const why = [];
  if (!n) why.push('Tick at least one item in the list.');
  if (!anyFmt) why.push('Turn on MP4 or PDF.');
  btn.disabled = S.busy || why.length > 0;
  $('make-wrap').classList.toggle('blocked', !S.busy && why.length > 0);
  $('make-tip').replaceChildren(h('b', { text: 'Not ready yet' }), ...why.map((w) => h('div', { text: w })));
  for (const [id, key] of [['fmt-mp4', 'mp4'], ['fmt-pdf', 'pdf']]) { $(id).classList.toggle('on', !!S.cfg[key]); $(id).setAttribute('aria-pressed', S.cfg[key] ? 'true' : 'false'); $(id).disabled = S.busy; }
  $('pv-opts').hidden = !S.cfg.mp4;                           // timing only applies to the MP4
  const parts = (S.cfg.outdir || '').split('/').filter(Boolean);
  $('out-path').textContent = S.cfg.outdir ? 'Last export: ' + (parts.length > 2 ? '…/' + parts.slice(-2).join('/') : S.cfg.outdir) : 'You choose the folder when you export';
  $('out-path').title = (S.cfg.outdir ? 'Last export folder: ' + S.cfg.outdir + '\n\n' : '') + 'You do not type a file name. Each file is named after its item, for example “261108 Warta.mp4” and “261108 Warta.pdf”. A file with the same name in the folder you choose is replaced.';
  O.all.set(S.cfg.allSeconds);
  $('all-on').checked = !!S.cfg.overrideAll; $('no-vhold').checked = !!S.cfg.noVideoHold;
  for (const id of ['all-on', 'no-vhold']) $(id).disabled = S.busy;
  O.all.field.disabled = O.all.field.nextSibling.disabled = S.busy;
}

// timing options: saved, and the open preview is rebuilt so its total time follows
let timingTimer = null;
function timingChanged(patch) {
  window.pp.cfgSet(patch).then((c) => { S.cfg = c; updateMake(); });
  S.cfg = { ...S.cfg, ...patch };
  clearTimeout(timingTimer);
  timingTimer = setTimeout(() => { if (S.focus && S.playlist) focusItem(S.playlist.items.find((i) => i.index === S.focus)); }, 500);
}

// format tabs: click to turn a format on or off, like ProPresenter's tab bars
for (const [id, key] of [['fmt-mp4', 'mp4'], ['fmt-pdf', 'pdf']]) {
  $(id).onclick = async () => { S.cfg = await window.pp.cfgSet({ [key]: !S.cfg[key] }); updateMake(); redrawPreview(); };
}
const refocus = () => { if (S.focus && S.playlist) focusItem(S.playlist.items.find((i) => i.index === S.focus)); };

$('open-pl').onclick = async () => {
  const files = await window.pp.pickProplaylist();
  if (!files.length) return;
  const recent = [...files, ...(S.cfg.recent || []).filter((x) => !files.includes(x))].slice(0, 12);
  S.cfg = await window.pp.cfgSet({ recent });
  for (const f of files) delete S.trees[f];
  await selectSource(files[0]);
};
$('refresh').onclick = async () => {
  delete S.trees[S.source]; delete S.phCache[S.source];
  const keep = S.playlist && S.playlist.uuid, focus = S.focus;
  await selectSource(S.source, true);
  const t = S.trees[S.source];
  if (keep && t && t.tree) { const pl = flatPlaylists(t.tree).find((p) => p.uuid === keep); if (pl) { S.playlist = pl; S.focus = focus; renderTree(); renderItems(); } }
};
$('filter').oninput = renderTree;

$('make').onclick = async () => {
  if (!S.playlist || !S.selected.size) return;
  const dir = await window.pp.pickFolder(S.cfg.outdir, { title: 'Export', button: 'Export', message: 'Choose where to save. Files are named after each item (for example “261108 Warta.mp4”); a file with the same name is replaced.' });
  if (!dir) return;
  S.cfg = await window.pp.cfgSet({ outdir: dir });
  const items = S.playlist.items.filter((i) => S.selected.has(i.index)).map((i) => ({ index: i.index, name: i.name, settings: settingsFor(i.index) }));
  S.busy = true; updateMake();
  $('cancel').hidden = false; $('prog').hidden = false; $('result').hidden = true; $('prog-fill').style.width = '0';
  $('prog-text').textContent = 'Starting…';
  const results = await window.pp.convert({ proplaylist: S.source || null, playlist: S.playlist.uuid, items, format: formatValue(), outdir: dir, timing: timing() });
  S.busy = false; $('cancel').hidden = true; updateMake();
  showResults(results);
};
$('cancel').onclick = () => window.pp.cancel();

window.pp.onProgress((p) => {
  const frac = p.total ? Math.min(1, (p.n + (p.done || 0)) / p.total) : 0;
  $('prog-fill').style.width = (frac * 100).toFixed(1) + '%';
  $('prog-text').textContent = p.stage === 'done' ? 'Finished.' : `${p.n + 1} of ${p.total}: ${p.name || ''}, ${p.stage || ''}`;
});

function showResults(results) {
  const box = $('result'); box.replaceChildren(); box.hidden = false;
  for (const r of results) {
    if (r.error) { box.append(h('div', { class: 'row' }, h('span', { class: 'bad', text: 'Failed' }), h('span', { text: `${r.item}: ${r.error}` }))); continue; }
    for (const f of r.files || []) {
      box.append(h('div', { class: 'row' }, h('span', { class: 'ok', text: 'Done' }), h('span', { text: baseName(f) }),
        h('a', { onclick: () => window.pp.reveal(f), text: 'Show in Finder' }), h('a', { onclick: () => window.pp.openPath(f), text: 'Open' })));
    }
    if (!(r.files || []).length) box.append(h('div', { class: 'row' }, h('span', { class: 'warn', text: 'Nothing written' }), h('span', { text: r.item })));
  }
}

// ------------------------------------------------------------------ start
(async function init() {
  S.cfg = { ...S.cfg, ...(await window.pp.cfgGet()) };
  if (S.cfg.theme) applyTheme(S.cfg.theme);
  IMP.template = S.cfg.importTemplate || ''; IMP.lines = S.cfg.importLines || 2; $('imp-lines').value = IMP.lines;
  const info = await window.pp.engineInfo();
  renderHelp(); renderSources(); updateMake();
  if (!info.ok) { $('tree').replaceChildren(h('div', { class: 'note err', text: 'Engine not found: ' + info.engine })); return; }
  await selectSource(LIB);
})();
