// Electron main process: owns the Python engine (pp2mp4.py) and everything that touches files.
// The engine only READS ProPresenter's files; this app never writes into the ProPresenter folder.
const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
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
const PP_ROOT = path.join(os.homedir(), 'Library/Application Support/RenewedVision/ProPresenter/UserWorkspaces/ProPresenter');

let win = null;
let current = null;          // the running convert child, so it can be cancelled
let cancelled = false;

// ---- small config file (last output folder, formats, recent .proplaylist files)
const cfgPath = () => path.join(app.getPath('userData'), 'config.json');
const CFG_DEFAULTS = { mp4: true, pdf: false, outdir: '', recent: [], pictureSeconds: 5, overrideAll: false, allSeconds: 10, noVideoHold: true };
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
ipcMain.handle('cfg:set', (_e, patch) => { const c = { ...readCfg(), ...patch }; writeCfg(c); return c; });

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

ipcMain.handle('pick-folder', async (_e, start) => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose where to save the MP4 / PDF files', properties: ['openDirectory', 'createDirectory'],
    defaultPath: start || app.getPath('desktop'), buttonLabel: 'Use this folder',
  });
  return r.canceled ? null : r.filePaths[0];
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
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
app.on('window-all-closed', () => { if (current) current.kill('SIGTERM'); if (process.platform !== 'darwin') app.quit(); });
