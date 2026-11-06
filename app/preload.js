const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pp', {
  cfgGet: () => ipcRenderer.invoke('cfg:get'),
  cfgSet: (patch) => ipcRenderer.invoke('cfg:set', patch),
  tree: (proplaylist) => ipcRenderer.invoke('tree', proplaylist),
  placeholders: (proplaylist) => ipcRenderer.invoke('placeholders', proplaylist),
  preview: (opts) => ipcRenderer.invoke('preview', opts),
  convert: (opts) => ipcRenderer.invoke('convert', opts),
  cancel: () => ipcRenderer.invoke('cancel'),
  pickFolder: (start) => ipcRenderer.invoke('pick-folder', start),
  pickProplaylist: () => ipcRenderer.invoke('pick-proplaylist'),
  pickBackground: () => ipcRenderer.invoke('pick-background'),
  reveal: (p) => ipcRenderer.invoke('reveal', p),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  engineInfo: () => ipcRenderer.invoke('engine-info'),
  onProgress: (cb) => ipcRenderer.on('progress', (_e, p) => cb(p)),
});
