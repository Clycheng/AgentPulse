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
import { createHash } from 'node:crypto';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
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

type LocalProject = {
  id: string;
  root: string;
  displayName: string;
  allowedScopes: string[];
};

type LocalWorkerState = {
  deviceId: string;
  deviceToken: string;
  projects: LocalProject[];
  lastError: string;
};

let localWorkerTimer: NodeJS.Timeout | null = null;
let localHeartbeatTimer: NodeJS.Timeout | null = null;
let localWorkerBusy = false;
let localWorkerState: LocalWorkerState | null = null;

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

function localWorkerStatePath() {
  return path.join(app.getPath('userData'), 'local-worker.json');
}

function readLocalWorkerState(): LocalWorkerState | null {
  if (!safeStorage.isEncryptionAvailable()) return null;
  try {
    const payload = JSON.parse(fs.readFileSync(localWorkerStatePath(), 'utf8')) as {
      value: string;
    };
    const value = JSON.parse(
      safeStorage.decryptString(Buffer.from(payload.value, 'base64')),
    ) as LocalWorkerState;
    if (!value.deviceId || !value.deviceToken || !Array.isArray(value.projects)) {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

function writeLocalWorkerState(value: LocalWorkerState) {
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('System credential storage is unavailable');
  }
  fs.mkdirSync(path.dirname(localWorkerStatePath()), { recursive: true });
  const temporary = `${localWorkerStatePath()}.tmp`;
  fs.writeFileSync(
    temporary,
    JSON.stringify({
      value: safeStorage.encryptString(JSON.stringify(value)).toString('base64'),
    }),
    { mode: 0o600 },
  );
  fs.renameSync(temporary, localWorkerStatePath());
  fs.chmodSync(localWorkerStatePath(), 0o600);
}

function localApiBaseUrl() {
  return isDev
    ? (process.env.VITE_AGENTPULSE_API_URL ?? 'http://127.0.0.1:8000/api')
    : 'https://api.agentpulse.cc/api';
}

async function localApiRequest<T>(
  apiPath: string,
  token: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${localApiBaseUrl()}${apiPath}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...(init.headers ?? {}),
    },
  });
  const body = (await response.json().catch(() => ({}))) as { detail?: string } & T;
  if (!response.ok) {
    throw new Error(body.detail || `Local Worker API error ${response.status}`);
  }
  return body;
}

function localHermesBinary() {
  if (process.env.AGENTPULSE_HERMES_BIN) return process.env.AGENTPULSE_HERMES_BIN;
  const bundled = path.join(
    process.resourcesPath,
    'hermes-runtime',
    process.platform === 'win32' ? 'hermes.exe' : 'hermes',
  );
  if (fs.existsSync(bundled)) return bundled;
  return process.platform === 'win32' ? 'hermes.exe' : 'hermes';
}

function localProjectForRun(run: Record<string, unknown>) {
  return localWorkerState?.projects.find(
    (project) => project.id === run.local_project_id,
  );
}

function redactLocalOutput(value: string, root: string) {
  return value.replaceAll(root, '[已授权本机项目路径]');
}

function readOnlyLocalRequest(prompt: string) {
  return !/(写入|修改|删除|创建文件|运行|执行命令|终端|git\s+push|发送邮件|发布)/i.test(
    prompt,
  );
}

async function postLocalEvent(
  token: string,
  runId: string,
  eventSeq: number,
  type: string,
  detail: string,
) {
  await localApiRequest(`/runs/${runId}/events`, token, {
    method: 'POST',
    body: JSON.stringify({
      event_seq: eventSeq,
      type,
      status: type === 'status' ? 'running' : '',
      title: type === 'status' ? '本机 Hermes' : '',
      detail,
      payload: {},
    }),
  });
}

async function executeLocalRun(run: Record<string, unknown>) {
  if (!localWorkerState) return;
  const project = localProjectForRun(run);
  const runId = String(run.id || '');
  if (!runId) return;
  if (!project) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({ error: '本机 Worker 找不到 Run 绑定的授权项目。' }),
    });
    return;
  }
  const prompt = String(run.prompt_text || '');
  if (!readOnlyLocalRequest(prompt)) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({
        error: '本机 Worker 当前只允许已授权项目内的只读分析；写文件和命令执行需要审批运行时。',
      }),
    });
    return;
  }
  if (!path.isAbsolute(project.root) || !fs.existsSync(project.root)) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({ error: '本机授权项目目录不存在。' }),
    });
    return;
  }
  const profile = String(run.hermes_profile_id || '');
  if (!profile) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({ error: '当前员工没有可用的本机 Hermes profile。' }),
    });
    return;
  }
  await localApiRequest(`/runs/${runId}/lease`, localWorkerState.deviceToken, {
    method: 'POST',
  });
  await postLocalEvent(
    localWorkerState.deviceToken,
    runId,
    1,
    'status',
    'Worker 已接管，正在授权项目内执行只读分析。',
  );

  const child: ChildProcessWithoutNullStreams = spawn(
    localHermesBinary(),
    ['--profile', profile, '--safe-mode', '-z', prompt],
    {
      cwd: project.root,
      env: { ...process.env, NO_COLOR: '1' },
      windowsHide: true,
    },
  );
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk: Buffer) => {
    stdout += chunk.toString('utf8');
    if (stdout.length > 60_000) stdout = stdout.slice(-60_000);
  });
  child.stderr.on('data', (chunk: Buffer) => {
    stderr += chunk.toString('utf8');
    if (stderr.length > 10_000) stderr = stderr.slice(-10_000);
  });
  const exitCode = await new Promise<number>((resolve) => {
    child.once('error', () => resolve(127));
    child.once('close', (code) => resolve(code ?? 1));
  });
  const output = redactLocalOutput(stdout.trim(), project.root);
  if (exitCode !== 0) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({
        error: `本机 Hermes 执行失败（退出码 ${exitCode}）：${redactLocalOutput(stderr.trim(), project.root).slice(-2000)}`,
      }),
    });
    return;
  }
  await postLocalEvent(localWorkerState.deviceToken, runId, 2, 'final', output);
  await localApiRequest(`/runs/${runId}/complete`, localWorkerState.deviceToken, {
    method: 'POST',
    body: JSON.stringify({ message: output, usage: {} }),
  });
}

async function pollLocalRuns() {
  if (localWorkerBusy || !localWorkerState) return;
  localWorkerBusy = true;
  try {
    const payload = await localApiRequest<{ run?: Record<string, unknown> }>(
      `/local-devices/${localWorkerState.deviceId}/runs/next`,
      localWorkerState.deviceToken,
    );
    if (payload.run) await executeLocalRun(payload.run);
  } catch (error) {
    localWorkerState.lastError = error instanceof Error ? error.message : String(error);
    try {
      writeLocalWorkerState(localWorkerState);
    } catch {
      // Keep the worker alive; status IPC exposes the last error.
    }
  } finally {
    localWorkerBusy = false;
  }
}

async function startLocalWorker() {
  const session = readStoredSession();
  if (!session) return { online: false, reason: '未登录' };
  const device = await localApiRequest<{ id: string; device_token: string }>(
    '/local-devices/register',
    session.accessToken,
    {
      method: 'POST',
      body: JSON.stringify({
        device_name: `${app.getName()} 本机`,
        platform: process.platform,
        architecture: process.arch,
        worker_version: app.getVersion(),
        hermes_version: '0.18.2',
        capabilities: { read_file: true, write_file: false, terminal: false, computer_use: false },
      }),
    },
  );
  const previous = readLocalWorkerState();
  localWorkerState = {
    deviceId: device.id,
    deviceToken: device.device_token,
    projects: previous?.projects ?? [],
    lastError: '',
  };
  writeLocalWorkerState(localWorkerState);
  if (localWorkerTimer) clearInterval(localWorkerTimer);
  if (localHeartbeatTimer) clearInterval(localHeartbeatTimer);
  localWorkerTimer = setInterval(() => void pollLocalRuns(), 2000);
  localHeartbeatTimer = setInterval(async () => {
    if (!localWorkerState) return;
    try {
      const heartbeat = await localApiRequest<{ device_token: string }>(
        `/local-devices/${localWorkerState.deviceId}/heartbeat`,
        localWorkerState.deviceToken,
        { method: 'POST', body: JSON.stringify({ hermes_version: '0.18.2' }) },
      );
      localWorkerState.deviceToken = heartbeat.device_token;
      writeLocalWorkerState(localWorkerState);
    } catch (error) {
      localWorkerState.lastError = error instanceof Error ? error.message : String(error);
    }
  }, 15_000);
  return { online: true, deviceId: device.id, hermes: localHermesBinary() };
}

function registerLocalRuntimeIpc() {
  ipcMain.handle('agentpulse:local-runtime:start', () => startLocalWorker());
  ipcMain.handle('agentpulse:local-runtime:status', () => ({
    online: Boolean(localWorkerState),
    deviceId: localWorkerState?.deviceId ?? null,
    hermes: localHermesBinary(),
    projects: localWorkerState?.projects.map(({ root: _root, ...project }) => project) ?? [],
    lastError: localWorkerState?.lastError ?? '',
  }));
  ipcMain.handle('agentpulse:local-project:pick', async () => {
    const session = readStoredSession();
    if (!session) throw new Error('请先登录');
    if (!localWorkerState) await startLocalWorker();
    if (!localWorkerState) throw new Error('本机 Worker 启动失败');
    const result = await dialog.showOpenDialog({
      title: '授权 AgentPulse 读取项目目录',
      properties: ['openDirectory'],
    });
    if (result.canceled || !result.filePaths[0]) return null;
    const root = path.resolve(result.filePaths[0]);
    const pathHash = createHash('sha256').update(root).digest('hex');
    const project = await localApiRequest<{ id: string }>(
      '/local-projects',
      session.accessToken,
      {
        method: 'POST',
        body: JSON.stringify({
          device_id: localWorkerState.deviceId,
          display_name: path.basename(root),
          path_hash: pathHash,
          allowed_scopes: ['read'],
        }),
      },
    );
    localWorkerState.projects = [
      ...localWorkerState.projects.filter((item) => item.id !== project.id),
      { id: project.id, root, displayName: path.basename(root), allowedScopes: ['read'] },
    ];
    writeLocalWorkerState(localWorkerState);
    return { id: project.id, display_name: path.basename(root), path_hash: pathHash };
  });
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
  registerLocalRuntimeIpc();
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
