import { contextBridge, ipcRenderer } from 'electron';

type SessionPayload = {
  accessToken: string;
  user: { id: string; email: string; display_name: string };
};

type ManagedObsidianDocument = {
  relative_path: string;
  title: string;
  content: string;
  modified_at: string;
};

contextBridge.exposeInMainWorld('agentpulse', {
  platform: process.platform,
  session: {
    get: () => ipcRenderer.invoke('agentpulse:session:get'),
    set: (value: SessionPayload) =>
      ipcRenderer.invoke('agentpulse:session:set', value),
    clear: () => ipcRenderer.invoke('agentpulse:session:clear'),
  },
  localRuntime: {
    start: () => ipcRenderer.invoke('agentpulse:local-runtime:start'),
    status: () => ipcRenderer.invoke('agentpulse:local-runtime:status'),
    pickProject: () => ipcRenderer.invoke('agentpulse:local-project:pick'),
  },
  obsidian: {
    pickManaged: (): Promise<{
      vault_name: string;
      managed_area: string;
      documents: ManagedObsidianDocument[];
    } | null> => ipcRenderer.invoke('agentpulse:obsidian:pick-managed'),
  },
});
