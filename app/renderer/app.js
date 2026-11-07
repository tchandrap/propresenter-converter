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

const LIB = '';                           // source id for ProPresenter's own library
const S = {
  cfg: { mp4: true, pdf: false, outdir: '', bg: '', recent: [], pictureSeconds: 5, overrideAll: false, allSeconds: 10, noVideoHold: true },
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
const timing = () => ({ pictureSeconds: S.cfg.pictureSeconds, overrideAll: !!S.cfg.overrideAll, allSeconds: S.cfg.allSeconds, noVideoHold: !!S.cfg.noVideoHold });
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
  if (t.error) { box.append(h('div', { class: 'note err', text: t.error })); return; }
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
  const r = await window.pp.preview({ proplaylist: S.source || null, playlist: S.playlist.uuid, index: it.index, bg: S.cfg.bg || '', timing: timing(), settings: settingsFor(it.index) });
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
  $('pv-meta').textContent = `${r.slides.length} slides · ${Math.floor(total / 60)}:${String(Math.round(total % 60)).padStart(2, '0')} · ${r.subtitle}`;
  const standin = r.slides.some((s) => /stand-in/.test(s.desc));
  const grid = h('div', { class: 'pv-grid' });
  const cur = settingsFor(it.index);
  const change = (n, key, val) => {
    const all = { ...S.slideSettings }; const k = setKey(it.index);
    if (key === 'seconds' && S.cfg.overrideAll) {            // editing one slide ends "same time for every slide" but keeps the rest as they are
      const frozen = { ...(all[k] || {}) };
      for (const s of r.slides) if (s.kind !== 'audio') frozen[s.n] = { ...(frozen[s.n] || {}), seconds: s.seconds };
      all[k] = frozen;
      S.cfg = { ...S.cfg, overrideAll: false }; window.pp.cfgSet({ overrideAll: false }); updateMake();
    }
    all[k] = { ...(all[k] || {}), [n]: { ...((all[k] || {})[n] || {}), [key]: val } };
    S.slideSettings = all;
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => focusItem(it, true), 700);
  };
  const numIn = (cls, value, min, max, step, title, onch) => {
    const i = h('input', { class: cls, type: 'number', min, max, step, value, title });
    i.onchange = () => { const v = Math.max(min, Math.min(max, Number(i.value))); if (Number.isFinite(v)) { i.value = v; onch(v); } };
    return i;
  };
  for (const s of r.slides) {
    const col = s.color || '#3a3a40';
    const rgb = [1, 3, 5].map((i) => parseInt(col.slice(i, i + 2), 16));
    const fg = rgb.reduce((a, b) => a + b, 0) > 450 ? '#000' : '#fff';
    const secs = s.kind === 'audio' ? h('span', { class: 's', text: s.seconds ? s.seconds + 's' : '' })
      : h('span', { class: 's', title: 'How long this slide stays on screen in the MP4. A video shorter than this holds its last frame; longer is cut.' },
        numIn('secs', s.seconds, 0.5, 3600, 0.5, 'Seconds on screen', (v) => change(s.n, 'seconds', v)), 's');
    const ctl = [];
    if (S.cfg.pdf && (s.kind === 'video' || s.kind === 'slidevideo') && s.vlen > 0) {
      const max = Math.max(0, Math.floor((s.vlen - 0.1) * 10) / 10), val = s.poster == null ? 0 : Math.min(max, s.poster);
      const rng = h('input', { class: 'tl', type: 'range', min: 0, max, step: 0.1, value: val, title: 'Drag to choose which moment of the video is the PDF picture' });
      const box = numIn('input mini', val, 0, max, 0.1, 'Seconds into the video', (v) => { rng.value = v; change(s.n, 'poster', v); });
      rng.oninput = () => { box.value = rng.value; };
      rng.onchange = () => change(s.n, 'poster', Number(rng.value));
      ctl.push(h('div', { class: 'c' }, 'PDF picture at ', box, ' s of ' + s.vlen + ' s'), h('div', { class: 'c' }, rng));
    }
    grid.append(h('div', { class: 'slide' },
      h('img', { src: 'file://' + encodeURI(s.file).replace(/#/g, '%23'), alt: s.name, loading: 'lazy' }),
      h('div', { class: 'lab', style: `background:${col};color:${fg}` }, h('b', { text: String(s.n) }), h('span', { class: 't', text: s.name }), secs),
      ctl.length ? h('div', { class: 'ctl' }, ...ctl) : null));
  }
  const notes = [];
  if (standin) notes.push(h('div', { class: 'note warn', text: 'ProPresenter themes are not stored in the files, so slides that only have text are shown on a dark stand-in background. Choose a Background below (e.g. your theme video) to use that instead; the MP4 / PDF will use it too.' }));
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
    h('p', { text: 'ProPresenter keeps themes outside the presentation files. Text-only slides are therefore drawn on a dark stand-in background. Use “Background → Choose…” to pick the theme video or picture instead.' }),
    h('h3', { text: 'Safety' }),
    h('p', {}, 'This app only ', h('b', { text: 'reads' }), ' ProPresenter’s files. It never changes anything in the ProPresenter folder. Videos from a .proplaylist are unpacked into a cache folder (', h('code', { text: '~/Library/Caches/pp2mp4' }), ') that you can delete at any time.'));
}

// ------------------------------------------------------------------ tabs, bottom bar
function showTab(name) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.id === 'view-' + name));
}
document.querySelectorAll('.tab').forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });

function updateMake() {
  const n = S.selected.size;
  const btn = $('make');
  const anyFmt = S.cfg.mp4 || S.cfg.pdf;
  btn.textContent = n === 1 ? 'Make 1 item' : `Make ${n} items`;
  btn.disabled = S.busy || n === 0 || !S.cfg.outdir || !anyFmt;
  btn.title = !n ? 'Tick at least one item' : !S.cfg.outdir ? 'Choose an output folder first' : !anyFmt ? 'Tick MP4 or PDF' : '';
  $('fmt-mp4').checked = !!S.cfg.mp4; $('fmt-pdf').checked = !!S.cfg.pdf; $('fmt-mp4').disabled = $('fmt-pdf').disabled = S.busy;
  const parts = (S.cfg.outdir || '').split('/').filter(Boolean);
  $('out-path').textContent = S.cfg.outdir ? (parts.length > 2 ? '…/' + parts.slice(-2).join('/') : S.cfg.outdir) : 'no folder chosen yet'; $('out-path').title = S.cfg.outdir || '';
  $('bg-name').textContent = S.cfg.bg ? baseName(S.cfg.bg) : 'stand-in (dark)'; $('clear-bg').hidden = !S.cfg.bg;
  if (document.activeElement !== $('pic-sec')) $('pic-sec').value = S.cfg.pictureSeconds;
  if (document.activeElement !== $('all-sec')) $('all-sec').value = S.cfg.allSeconds;
  $('all-on').checked = !!S.cfg.overrideAll; $('no-vhold').checked = !!S.cfg.noVideoHold;
  for (const id of ['pic-sec', 'all-on', 'all-sec', 'no-vhold']) $(id).disabled = S.busy;
}

// timing options: saved, and the open preview is rebuilt so its total time follows
let timingTimer = null;
function timingChanged(patch) {
  window.pp.cfgSet(patch).then((c) => { S.cfg = c; updateMake(); });
  S.cfg = { ...S.cfg, ...patch };
  clearTimeout(timingTimer);
  timingTimer = setTimeout(() => { if (S.focus && S.playlist) focusItem(S.playlist.items.find((i) => i.index === S.focus)); }, 500);
}
$('pic-sec').onchange = () => { const v = Math.max(1, Math.min(600, Math.round(Number($('pic-sec').value) || 5))); $('pic-sec').value = v; timingChanged({ pictureSeconds: v }); };
$('all-sec').onchange = () => { const v = Math.max(0.5, Math.min(3600, Number($('all-sec').value) || 10)); $('all-sec').value = v; timingChanged({ allSeconds: v }); };
$('all-on').onchange = () => {
  if ($('all-on').checked) {                               // ticking applies the time to every slide: forget the times set one by one
    const all = {};
    for (const [k, slides] of Object.entries(S.slideSettings)) {
      const kept = {};
      for (const [n, v] of Object.entries(slides)) { const { seconds, ...rest } = v; if (Object.keys(rest).length) kept[n] = rest; }
      if (Object.keys(kept).length) all[k] = kept;
    }
    S.slideSettings = all;
  }
  timingChanged({ overrideAll: $('all-on').checked });
};
$('no-vhold').onchange = () => timingChanged({ noVideoHold: $('no-vhold').checked });

$('fmt-mp4').onchange = async () => { S.cfg = await window.pp.cfgSet({ mp4: $('fmt-mp4').checked }); updateMake(); };
$('fmt-pdf').onchange = async () => { S.cfg = await window.pp.cfgSet({ pdf: $('fmt-pdf').checked }); updateMake(); redrawPreview(); };
$('pick-out').onclick = async () => { const d = await window.pp.pickFolder(S.cfg.outdir); if (d) { S.cfg = await window.pp.cfgSet({ outdir: d }); updateMake(); } };
const refocus = () => { if (S.focus && S.playlist) focusItem(S.playlist.items.find((i) => i.index === S.focus)); };
$('pick-bg').onclick = async () => { const f = await window.pp.pickBackground(); if (f) { S.cfg = await window.pp.cfgSet({ bg: f }); updateMake(); refocus(); } };
$('clear-bg').onclick = async () => { S.cfg = await window.pp.cfgSet({ bg: '' }); updateMake(); refocus(); };

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
  const items = S.playlist.items.filter((i) => S.selected.has(i.index)).map((i) => ({ index: i.index, name: i.name, settings: settingsFor(i.index) }));
  S.busy = true; updateMake();
  $('cancel').hidden = false; $('prog').hidden = false; $('result').hidden = true; $('prog-fill').style.width = '0';
  $('prog-text').textContent = 'Starting…';
  const results = await window.pp.convert({ proplaylist: S.source || null, playlist: S.playlist.uuid, items, format: formatValue(), outdir: S.cfg.outdir, bg: S.cfg.bg || '', timing: timing() });
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
  const info = await window.pp.engineInfo();
  renderHelp(); renderSources(); updateMake();
  if (!info.ok) { $('tree').replaceChildren(h('div', { class: 'note err', text: 'Engine not found: ' + info.engine })); return; }
  await selectSource(LIB);
})();
