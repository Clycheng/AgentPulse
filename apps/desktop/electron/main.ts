import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  net,
  protocol,
  safeStorage,
  session,
} from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = process.env.VITE_DEV_SERVER_URL !== undefined;
const appOrigin = 'app://agentpulse';

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'app',
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
    },
  },
]);

type StoredSession = {
  accessToken: string;
  user: {
    id: string;
    email: string;
    display_name: string;
  };
};

function isStoredSession(value: unknown): value is StoredSession {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.accessToken !== 'string' ||
    candidate.accessToken.length < 20 ||
    candidate.accessToken.length > 8192
  ) {
    return false;
  }
  if (!candidate.user || typeof candidate.user !== 'object') return false;
  const user = candidate.user as Record<string, unknown>;
  return (
    typeof user.id === 'string' &&
    user.id.length <= 128 &&
    typeof user.email === 'string' &&
    user.email.length <= 255 &&
    typeof user.display_name === 'string' &&
    user.display_name.length <= 80
  );
}

function sessionPath() {
  return path.join(app.getPath('userData'), 'session.json');
}

function readStoredSession(): StoredSession | null {
  const file = sessionPath();
  if (!fs.existsSync(file) || !safeStorage.isEncryptionAvailable()) return null;
  try {
    const payload = JSON.parse(fs.readFileSync(file, 'utf8')) as {
      token: string;
      user: StoredSession['user'];
    };
    const stored = {
      accessToken: safeStorage.decryptString(
        Buffer.from(payload.token, 'base64'),
      ),
      user: payload.user,
    };
    return isStoredSession(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredSession(value: StoredSession) {
  if (!isStoredSession(value)) {
    throw new Error('Invalid session payload');
  }
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('System credential storage is unavailable');
  }
  const file = sessionPath();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(
    temporary,
    JSON.stringify({
      token: safeStorage.encryptString(value.accessToken).toString('base64'),
      user: value.user,
    }),
    { mode: 0o600 },
  );
  fs.renameSync(temporary, file);
  fs.chmodSync(file, 0o600);
}

function clearStoredSession() {
  try {
    fs.rmSync(sessionPath(), { force: true });
  } catch {
    // Logout remains successful even if the already-missing file races us.
  }
}

function registerSessionIpc() {
  ipcMain.handle('agentpulse:session:get', () => readStoredSession());
  ipcMain.handle('agentpulse:session:set', (_event, value: StoredSession) => {
    writeStoredSession(value);
    return true;
  });
  ipcMain.handle('agentpulse:session:clear', () => {
    clearStoredSession();
    return true;
  });
}

type ManagedObsidianDocument = {
  relative_path: string;
  title: string;
  content: string;
  modified_at: string;
};

function markdownTitle(filePath: string, content: string): string {
  const heading = content.match(/^#\s+(.+)$/m)?.[1]?.trim();
  return heading || path.basename(filePath, path.extname(filePath));
}

function readManagedObsidianDocuments(vaultPath: string): ManagedObsidianDocument[] {
  const managedRoot = path.resolve(vaultPath, '.agentpulse', 'managed');
  if (!fs.existsSync(managedRoot) || !fs.statSync(managedRoot).isDirectory()) {
    return [];
  }
  const documents: ManagedObsidianDocument[] = [];
  const visit = (directory: string) => {
    if (documents.length >= 200) return;
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (documents.length >= 200) return;
      const absolute = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        visit(absolute);
        continue;
      }
      if (!entry.isFile() || path.extname(entry.name).toLowerCase() !== '.md') continue;
      const stat = fs.statSync(absolute);
      if (stat.size > 100_000) continue;
      const content = fs.readFileSync(absolute, 'utf8').trim();
      if (!content) continue;
      documents.push({
        relative_path: path.relative(managedRoot, absolute).split(path.sep).join('/'),
        title: markdownTitle(absolute, content),
        content,
        modified_at: stat.mtime.toISOString(),
      });
    }
  };
  visit(managedRoot);
  return documents;
}

function registerObsidianIpc() {
  ipcMain.handle('agentpulse:obsidian:pick-managed', async () => {
    const result = await dialog.showOpenDialog({
      title: '选择 Obsidian Vault',
      properties: ['openDirectory'],
    });
    if (result.canceled || !result.filePaths[0]) return null;
    const vaultPath = result.filePaths[0];
    return {
      vault_name: path.basename(vaultPath),
      managed_area: '.agentpulse/managed',
      documents: readManagedObsidianDocuments(vaultPath),
    };
  });
}

function registerAppProtocol() {
  protocol.handle('app', (request) => {
    const url = new URL(request.url);
    const relativePath =
      decodeURIComponent(url.pathname).replace(/^\/+/, '') || 'index.html';
    const root = path.resolve(__dirname, '../dist');
    const requested = path.resolve(root, relativePath);
    if (requested !== root && !requested.startsWith(`${root}${path.sep}`)) {
      return new Response('Not found', { status: 404 });
    }
    return net.fetch(pathToFileURL(requested).toString());
  });
}

async function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1040,
    minHeight: 640,
    title: 'AgentPulse',
    backgroundColor: '#f3f4f6',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  mainWindow.webContents.on('did-fail-load', (_event, code, description) => {
    console.error(`Renderer failed to load (${code}): ${description}`);
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    console.error(`Renderer exited: ${details.reason}`);
  });

  const allowedOrigin = isDev
    ? new URL(process.env.VITE_DEV_SERVER_URL!).origin
    : '';
  mainWindow.webContents.on('will-navigate', (event, targetUrl) => {
    const target = new URL(targetUrl);
    const allowed = isDev
      ? target.origin === allowedOrigin
      : target.protocol === 'app:' && target.host === 'agentpulse';
    if (!allowed) event.preventDefault();
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  mainWindow.webContents.on('will-attach-webview', (event) =>
    event.preventDefault(),
  );

  if (isDev) {
    await mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL!);
    mainWindow.webContents.openDevTools({ mode: 'detach' });
    return;
  }

  await mainWindow.loadURL(`${appOrigin}/index.html`);
}

app.whenReady().then(async () => {
  registerSessionIpc();
  registerObsidianIpc();
  registerAppProtocol();
  session.defaultSession.setPermissionRequestHandler(
    (_webContents, _permission, callback) => {
      callback(false);
    },
  );
  await createWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    void createWindow();
  }
});
