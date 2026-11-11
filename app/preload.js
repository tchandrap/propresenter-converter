const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pp', {
  cfgGet: () => ipcRenderer.invoke('cfg:get'),
  cfgSet: (patch) => ipcRenderer.invoke('cfg:set', patch),
  tree: (proplaylist) => ipcRenderer.invoke('tree', proplaylist),
  placeholders: (proplaylist) => ipcRenderer.invoke('placeholders', proplaylist),
  preview: (opts) => ipcRenderer.invoke('preview', opts),
  convert: (opts) => ipcRenderer.invoke('convert', opts),
  cancel: () => ipcRenderer.invoke('cancel'),
  pickFolder: (start, o) => ipcRenderer.invoke('pick-folder', start, o),
  pickProplaylist: () => ipcRenderer.invoke('pick-proplaylist'),
  reveal: (p) => ipcRenderer.invoke('reveal', p),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  engineInfo: () => ipcRenderer.invoke('engine-info'),
  pickInput: (kind) => ipcRenderer.invoke('pick-input', kind),
  importRun: (o) => ipcRenderer.invoke('import-run', o),
  importSave: (o) => ipcRenderer.invoke('import-save', o),
  ppInfo: () => ipcRenderer.invoke('pp:info'),
  ppPick: () => ipcRenderer.invoke('pp:pick'),
  onProgress: (cb) => ipcRenderer.on('progress', (_e, p) => cb(p)),
});
