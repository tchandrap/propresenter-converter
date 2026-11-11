// Electron main process: owns the Python engine (pp2mp4.py) and everything that touches files.
// The engine only READS ProPresenter's files; this app never writes into the ProPresenter folder.
const { app, BrowserWindow, ipcMain, dialog, shell, nativeTheme } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { spawn } = require('child_process');

const ENGINE_DIR = process.env.PP2MP4_DIR || path.resolve(__dirname, '..', 'engine');
const DEV_PY = path.resolve(__dirname, '..', '.venv', 'bin', 'python');
const ENGINE = path.join(ENGINE_DIR, 'pp2mp4.py');
const PY = process.env.PP2MP4_PYTHON ||
  (fs.existsSync(DEV_PY) ? DEV_PY : 'python3');
// Packaged app: the engine is a frozen executable in Resources/engine, with a precompiled text renderer.
const BUNDLED_ENGINE = process.resourcesPath ? path.join(process.resourcesPath, 'pp2mp4-engine', 'pp2mp4-engine') : '';
const BUNDLED_RTF = process.resourcesPath ? path.join(process.resourcesPath, 'rtf2png') : '';
const USE_BUNDLED = app.isPackaged && fs.existsSync(BUNDLED_ENGINE);
const PREVIEW_ROOT = path.join(os.tmpdir(), 'pp2mp4-app-previews');
const PP_DEFAULT = path.join(os.homedir(), 'Library/Application Support/RenewedVision/ProPresenter/UserWorkspaces/ProPresenter');
// A ProPresenter workspace folder holds Libraries and Playlists. Accept the folder itself or a parent a level or two above it.
const isWorkspace = (d) => { try { return fs.existsSync(path.join(d, 'Libraries')) || fs.existsSync(path.join(d, 'Playlists')); } catch { return false; } };
const resolveWorkspace = (d) => [d, path.join(d, 'ProPresenter'), path.join(d, 'UserWorkspaces', 'ProPresenter'), path.join(d, 'ProPresenter', 'UserWorkspaces', 'ProPresenter')].find(isWorkspace) || null;

let win = null;
let current = null;          // the running convert child, so it can be cancelled
let cancelled = false;

// ---- small config file (last output folder, formats, recent .proplaylist files)
const cfgPath = () => path.join(app.getPath('userData'), 'config.json');
const CFG_DEFAULTS = { mp4: true, pdf: false, outdir: '', recent: [], ppRoot: '', theme: '', importDir: '', importTemplate: '', importLines: 2, pictureSeconds: 5, overrideAll: false, allSeconds: 10, noVideoHold: true };
function readCfg() {
  try {
    const saved = JSON.parse(fs.readFileSync(cfgPath(), 'utf8'));
    if (saved.format && saved.mp4 === undefined) { saved.mp4 = saved.format !== 'pdf'; saved.pdf = saved.format !== 'mp4'; }   // older settings: one format
    return { ...CFG_DEFAULTS, ...saved };
  } catch { return { ...CFG_DEFAULTS }; }
}
function writeCfg(c) { try { fs.mkdirSync(path.dirname(cfgPath()), { recursive: true }); fs.writeFileSync(cfgPath(), JSON.stringify(c, null, 1)); } catch { /* ignore */ } }

// ---- run the engine; collects stdout/stderr, forwards @@PROGRESS lines
function runEngine(args, { onProgress, track } = {}) {
  return new Promise((resolve) => {
    const cmd = USE_BUNDLED ? BUNDLED_ENGINE : PY;
    const root = readCfg().ppRoot;                         // the user's own ProPresenter folder, if they set one in Settings
    if (root) args = ['--pp-root', root, ...args];
    const argv = USE_BUNDLED ? args : ['-u', ENGINE, ...args];
    const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' };
    if (USE_BUNDLED) env.PP2MP4_RTF2PNG = BUNDLED_RTF;
    const child = spawn(cmd, argv, { cwd: USE_BUNDLED ? os.homedir() : ENGINE_DIR, env });
    if (track) current = child;
    let out = '', err = '', buf = '';
    child.stdout.on('data', (d) => {
      const s = d.toString();
      out += s; buf += s;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (onProgress && line.startsWith('@@PROGRESS ')) { try { onProgress(JSON.parse(line.slice(11))); } catch { /* ignore */ } }
      }
    });
    child.stderr.on('data', (d) => { err += d.toString(); });
    child.on('error', (e) => resolve({ code: -1, out, err: String(e) }));
    child.on('close', (code) => { if (current === child) current = null; resolve({ code, out, err }); });
  });
}

function lastLine(s) { const l = s.trim().split('\n').filter(Boolean); return l[l.length - 1] || ''; }

// Per-slide choices from the preview (seconds on screen, video frame for stills / PDF): passed to the engine as a JSON file.
function settingsArgs(settings) {
  if (!settings || !Object.keys(settings).length) return [];
  const f = path.join(os.tmpdir(), `pp2mp4-settings-${crypto.randomBytes(6).toString('hex')}.json`);
  fs.writeFileSync(f, JSON.stringify(settings));
  return ['--slide-settings', f];
}

function srcArgs(proplaylist) { return proplaylist ? ['--proplaylist', proplaylist] : []; }

// Timing options from the UI: seconds per picture (used for slides without their own timer, or for every
// picture-only slide when `override` is on) and "go straight to the next slide after a video".
function timingArgs(t) {
  const a = [];
  const s = Number(t && t.pictureSeconds);
  if (s > 0 && s <= 600) a.push('--hold', String(s));
  const all = Number(t && t.allSeconds);
  if (t && t.overrideAll && all > 0 && all <= 3600) a.push('--all-seconds', String(all));
  if (t && t.noVideoHold) a.push('--no-video-hold');
  return a;
}

// ---- IPC
ipcMain.handle('cfg:get', () => readCfg());
ipcMain.handle('cfg:set', (_e, patch) => {
  const c = { ...readCfg(), ...patch }; writeCfg(c);
  if (patch && patch.theme) nativeTheme.themeSource = patch.theme;     // window frame and menus follow the chosen theme
  return c;
});

// ---- Settings: where ProPresenter keeps its files
ipcMain.handle('pp:info', () => {
  const custom = readCfg().ppRoot;
  const root = custom || PP_DEFAULT;
  return { def: PP_DEFAULT, root, custom: !!custom, found: isWorkspace(root), defaultFound: isWorkspace(PP_DEFAULT) };
});
ipcMain.handle('pp:pick', async () => {
  const start = fs.existsSync(path.dirname(PP_DEFAULT)) ? path.dirname(PP_DEFAULT) : os.homedir();
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose your ProPresenter folder', message: 'Pick the ProPresenter workspace folder: the one that has Libraries and Playlists inside.',
    properties: ['openDirectory'], defaultPath: readCfg().ppRoot || start, buttonLabel: 'Use this folder',
  });
  if (r.canceled) return null;
  const root = resolveWorkspace(r.filePaths[0]);
  return root ? { root } : { error: 'This does not look like a ProPresenter workspace folder. It should have a “Libraries” and a “Playlists” folder inside.' };
});

ipcMain.handle('tree', async (_e, proplaylist) => {
  const r = await runEngine(['--playlists', '--json', ...srcArgs(proplaylist)]);
  if (r.code !== 0) return { error: lastLine(r.err) || lastLine(r.out) || 'Could not read the playlists.' };
  try { return JSON.parse(r.out); } catch { return { error: 'The engine returned unreadable data.' }; }
});

ipcMain.handle('placeholders', async (_e, proplaylist) => {
  const r = await runEngine(['--placeholders', '--json', ...srcArgs(proplaylist)]);
  if (r.code !== 0) return { error: lastLine(r.err) || lastLine(r.out) || 'Could not read the placeholders.' };
  try { return JSON.parse(r.out); } catch { return { error: 'The engine returned unreadable data.' }; }
});

ipcMain.handle('preview', async (_e, { proplaylist, playlist, index, timing, settings }) => {
  const key = crypto.createHash('sha1').update(JSON.stringify([proplaylist || '', playlist, index, timing || null, settings || null])).digest('hex').slice(0, 16);
  const dir = path.join(PREVIEW_ROOT, key);
  fs.rmSync(dir, { recursive: true, force: true });
  const args = [...srcArgs(proplaylist), '--playlist', playlist, '--item', String(index), '--preview', dir, '--json', ...timingArgs(timing), ...settingsArgs(settings)];
  const r = await runEngine(args);
  if (r.code !== 0) return { error: lastLine(r.err) || lastLine(r.out) || 'Could not build the preview.' };
  try { return JSON.parse(r.out); } catch { return { error: 'The engine returned unreadable data.' }; }
});

ipcMain.handle('convert', async (_e, { proplaylist, playlist, items, format, outdir, timing }) => {
  cancelled = false;
  const results = [];
  fs.mkdirSync(outdir, { recursive: true });
  for (let n = 0; n < items.length; n++) {
    if (cancelled) { results.push({ item: items[n].name, error: 'Cancelled.' }); break; }
    const it = items[n];
    const args = [...srcArgs(proplaylist), '--playlist', playlist, '--item', String(it.index), '--outdir', outdir, '--json', '--progress', ...timingArgs(timing), ...settingsArgs(it.settings)];
    if (format === 'pdf') args.push('--pdf-only');
    else if (format === 'both') args.push('--pdf');
    win && win.webContents.send('progress', { n, total: items.length, name: it.name, done: 0, stage: 'starting' });
    const r = await runEngine(args, {
      track: true,
      onProgress: (p) => win && win.webContents.send('progress', { n, total: items.length, name: it.name, ...p }),
    });
    if (cancelled) { results.push({ item: it.name, error: 'Cancelled.' }); break; }
    if (r.code !== 0) { results.push({ item: it.name, error: lastLine(r.err) || 'Failed.' }); continue; }
    let written = [];
    for (const line of r.out.split('\n')) { if (line.startsWith('{"written"')) { try { written = JSON.parse(line).written; } catch { /* ignore */ } } }
    results.push({ item: it.name, files: written });
  }
  win && win.webContents.send('progress', { n: items.length, total: items.length, done: 1, stage: 'done' });
  return results;
});

ipcMain.handle('cancel', () => { cancelled = true; if (current) current.kill('SIGTERM'); });

ipcMain.handle('pick-folder', async (_e, start, o) => {
  o = o || {};
  const r = await dialog.showOpenDialog(win, {
    title: o.title || 'Choose a folder', message: o.message || undefined, properties: ['openDirectory', 'createDirectory'],
    defaultPath: start || app.getPath('desktop'), buttonLabel: o.button || 'Choose',
  });
  return r.canceled ? null : r.filePaths[0];
});

// ---- Import: build a playlist from a worship script (PDF) and PowerPoint decks
ipcMain.handle('pick-input', async (_e, kind) => {
  const spec = {
    pdf: { title: 'Choose the worship script (PDF)', filters: [{ name: 'PDF', extensions: ['pdf'] }] },
    pptx: { title: 'Choose a PowerPoint deck', filters: [{ name: 'PowerPoint', extensions: ['pptx'] }] },
    template: { title: 'Choose a finished playlist to copy the look from', filters: [{ name: 'ProPresenter playlist', extensions: ['proplaylist'] }] },
  }[kind];
  if (!spec) return null;
  const r = await dialog.showOpenDialog(win, { ...spec, properties: ['openFile'], defaultPath: readCfg().importDir || app.getPath('downloads') });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle('import-run', async (_e, o) => {
  const dir = path.join(os.tmpdir(), 'slidebridge-import');
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(dir, `${Date.now()}.proplaylist`);
  const args = ['--import-out', tmp, '--json', '--lines-per-slide', String(Math.max(1, Math.min(8, Number(o.lines) || 2)))];
  if (o.pdf) args.push('--import-script', o.pdf);
  if (o.khotbah) args.push('--import-khotbah', o.khotbah);
  if (o.master) args.push('--import-master', o.master);
  if (o.template) args.push('--template-playlist', o.template);
  const r = await runEngine(args, { track: true });
  if (r.code !== 0) return { error: lastLine(r.err) || lastLine(r.out) || 'Could not build the playlist.' };
  try { return { ...JSON.parse(r.out), tmp }; } catch { return { error: 'The engine returned unreadable data.' }; }
});

ipcMain.handle('import-save', async (_e, { tmp, name }) => {
  if (!tmp || !fs.existsSync(tmp)) return { error: 'The built playlist is gone. Create it again.' };
  const safe = String(name || 'KU').replace(/[\\/:*?"<>|]+/g, '-');
  const r = await dialog.showSaveDialog(win, {
    title: 'Save the playlist', defaultPath: path.join(readCfg().importDir || app.getPath('documents'), `${safe}.proplaylist`),
    buttonLabel: 'Save', filters: [{ name: 'ProPresenter playlist', extensions: ['proplaylist'] }],
    message: 'Open the saved file in ProPresenter: double-click it, or drag it onto the Playlists sidebar.',
  });
  if (r.canceled || !r.filePath) return null;
  const out = r.filePath.toLowerCase().endsWith('.proplaylist') ? r.filePath : r.filePath + '.proplaylist';
  try { fs.copyFileSync(tmp, out); } catch (e) { return { error: 'Could not save: ' + e.message }; }
  writeCfg({ ...readCfg(), importDir: path.dirname(out) });
  return { path: out };
});

ipcMain.handle('pick-proplaylist', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Open a .proplaylist file', properties: ['openFile', 'openDirectory', 'multiSelections'],
    filters: [{ name: 'ProPresenter playlist', extensions: ['proplaylist'] }],
    defaultPath: app.getPath('downloads'),
  });
  return r.canceled ? [] : r.filePaths.filter((p) => p.toLowerCase().endsWith('.proplaylist'));
});

ipcMain.handle('reveal', (_e, p) => { if (p && fs.existsSync(p)) shell.showItemInFolder(p); });
ipcMain.handle('open-path', (_e, p) => { if (p && fs.existsSync(p)) return shell.openPath(p); });
ipcMain.handle('engine-info', () => USE_BUNDLED
  ? ({ engine: BUNDLED_ENGINE, python: 'bundled', ok: true })
  : ({ engine: ENGINE, python: PY, ok: fs.existsSync(ENGINE) }));

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 820, minWidth: 980, minHeight: 620, title: 'SlideBridge', icon: path.join(__dirname, 'assets', 'icon.icns'),
    backgroundColor: '#1c1c1f',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (e) => e.preventDefault());
}

app.whenReady().then(() => {
  const t = readCfg().theme; if (t) nativeTheme.themeSource = t;
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('window-all-closed', () => { if (current) current.kill('SIGTERM'); if (process.platform !== 'darwin') app.quit(); });
