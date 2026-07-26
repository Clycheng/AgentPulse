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
import {
  createDecipheriv,
  createHash,
  createPublicKey,
  diffieHellman,
  generateKeyPairSync,
  hkdfSync,
} from 'node:crypto';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolveExplicitLocalProjectRoot } from './local-project-path.js';

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
  workspaceId: string;
  projects: LocalProject[];
  profilesReady: boolean;
  lastError: string;
};

type LocalHermesRuntime = {
  command: string;
  python: string;
  workerScript: string;
  label: string;
  mode: 'bundled' | 'development';
};

let localWorkerTimer: NodeJS.Timeout | null = null;
let localHeartbeatTimer: NodeJS.Timeout | null = null;
let localWorkerPolling = false;
const localWorkerRuns = new Map<string, Record<string, unknown>>();
const localWorkerMaxRuns = 4;
let localWorkerRestarting = false;
let localWorkerState: LocalWorkerState | null = null;
let localWorkerStartPromise: ReturnType<typeof startLocalWorkerInternal> | null = null;

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
    if (
      !value.deviceId ||
      !value.deviceToken ||
      !value.workspaceId ||
      !Array.isArray(value.projects)
    ) {
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

function bundledRuntimeRoot() {
  return isDev
    ? path.resolve(__dirname, '..', 'runtime-stage')
    : path.join(process.resourcesPath, 'hermes-runtime');
}

function resolveLocalHermesRuntime(): LocalHermesRuntime {
  if (isDev) {
    const runtimeRoot = bundledRuntimeRoot();
    const command = process.env.AGENTPULSE_HERMES_BIN || path.join(runtimeRoot, 'bin', 'hermes');
    const python = process.env.AGENTPULSE_LOCAL_WORKER_PYTHON || path.join(
      runtimeRoot,
      'python',
      process.platform === 'win32' ? 'python.exe' : 'bin/python3.11',
    );
    if (!fs.existsSync(command) || !fs.existsSync(python)) {
      throw new Error('开发 runtime-stage 缺失，请先运行 npm run prepare:runtime。');
    }
    return {
      command,
      python,
      workerScript: path.resolve(__dirname, '..', 'local-worker', 'agentpulse_local_worker.py'),
      label: process.env.AGENTPULSE_HERMES_BIN
        ? '开发覆盖 Hermes'
        : '开发环境 Hermes',
      mode: 'development',
    };
  }

  const runtimeRoot = bundledRuntimeRoot();
  const manifestPath = path.join(runtimeRoot, 'runtime.json');
  if (!fs.existsSync(manifestPath)) {
    throw new Error('安装包缺少内置 Hermes runtime，请重新下载安装包。');
  }
  let manifest: {
    platform?: string;
    architecture?: string;
    hermes?: { version?: string; commit?: string };
    launcher?: string;
    local_worker_sha256?: string;
  };
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch {
    throw new Error('内置 Hermes runtime 清单损坏，请重新下载安装包。');
  }
  if (
    manifest.platform !== process.platform ||
    manifest.architecture !== process.arch ||
    manifest.hermes?.version !== '0.18.2' ||
    manifest.hermes?.commit !== 'e4ea0a0ed7fc24761b2b425146893561a73216e1' ||
    !manifest.launcher ||
    path.isAbsolute(manifest.launcher) ||
    manifest.launcher.split(/[\\/]+/).includes('..')
  ) {
    throw new Error('内置 Hermes runtime 与当前客户端不匹配，请重新下载安装包。');
  }
  const command = path.resolve(runtimeRoot, manifest.launcher);
  const python = path.join(
    runtimeRoot,
    'python',
    process.platform === 'win32' ? 'python.exe' : 'bin/python3.11',
  );
  const workerScript = path.join(runtimeRoot, 'local-worker', 'agentpulse_local_worker.py');
  if (
    !command.startsWith(`${runtimeRoot}${path.sep}`) ||
    !fs.existsSync(command) ||
    !fs.existsSync(python) ||
    !fs.existsSync(workerScript) ||
    !manifest.local_worker_sha256 ||
    createHash('sha256').update(fs.readFileSync(workerScript)).digest('hex') !==
      manifest.local_worker_sha256
  ) {
    throw new Error('内置 Hermes 启动器缺失，请重新下载安装包。');
  }
  return {
    command,
    python,
    workerScript,
    label: 'Hermes v0.18.2（内置）',
    mode: 'bundled',
  };
}

type LocalProfileManifest = {
  agent_id: string;
  profile_name: string;
  name: string;
  role: string;
  soul: string;
  toolsets: string[];
  manifest_hash: string;
};

type LocalRuntimeBootstrap = {
  workspace_id: string;
  runtime: {
    hermes_version: string;
    model: string;
    model_configured: boolean;
  };
  profiles: LocalProfileManifest[];
};

const gatedToolsets = [
  'web', 'browser', 'terminal', 'file', 'code_execution', 'computer_use',
  'image_gen', 'video_gen', 'video', 'vision', 'x_search', 'tts',
  'homeassistant', 'spotify', 'yuanbao',
];

// These are the only Hermes toolsets with a Local Worker handler today.
// Authorization remains broader, but the profile must never advertise a tool
// that this installed client cannot execute and receipt.
const localExecutableToolsets = new Set(['file', 'terminal']);

function localHermesHome(workspaceId: string) {
  const namespace = createHash('sha256').update(workspaceId).digest('hex').slice(0, 24);
  return path.join(app.getPath('userData'), 'local-runtime', 'hermes', namespace);
}

function localWorkerWorkRoot(workspaceId: string) {
  return path.join(localHermesHome(workspaceId), 'agentpulse-work');
}

async function runHermesCommand(
  runtime: LocalHermesRuntime,
  args: string[],
  environment: NodeJS.ProcessEnv,
): Promise<void> {
  const child = spawn(runtime.command, args, {
    env: environment,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (chunk: Buffer) => {
    stderr = `${stderr}${chunk.toString('utf8')}`.slice(-2_000);
  });
  const exitCode = await new Promise<number>((resolve) => {
    const timeout = setTimeout(() => {
      child.kill();
      resolve(124);
    }, 30_000);
    child.once('error', () => {
      clearTimeout(timeout);
      resolve(127);
    });
    child.once('close', (code) => {
      clearTimeout(timeout);
      resolve(code ?? 1);
    });
  });
  if (exitCode !== 0) {
    throw new Error(stderr.trim() || `Hermes 命令失败（退出码 ${exitCode}）`);
  }
}

async function materializeLocalProfile(
  runtime: LocalHermesRuntime,
  workspaceId: string,
  model: string,
  manifest: LocalProfileManifest,
) {
  const hermesHome = localHermesHome(workspaceId);
  const profileHome = path.join(hermesHome, 'profiles', manifest.profile_name);
  const workdir = path.join(localWorkerWorkRoot(workspaceId), manifest.profile_name, 'work');
  fs.mkdirSync(workdir, { recursive: true, mode: 0o700 });
  const env = {
    ...process.env,
    HERMES_HOME: hermesHome,
    HERMES_PROFILE: manifest.profile_name,
    NO_COLOR: '1',
  };
  if (!fs.existsSync(profileHome)) {
    await runHermesCommand(
      runtime,
      ['profile', 'create', manifest.profile_name, '--no-alias', '--no-skills'],
      env,
    );
  }
  fs.mkdirSync(profileHome, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(profileHome, 'SOUL.md'), manifest.soul, {
    encoding: 'utf8',
    mode: 0o600,
  });
  const executableToolsets = manifest.toolsets.filter((toolset) =>
    localExecutableToolsets.has(toolset),
  );
  fs.writeFileSync(
    path.join(profileHome, 'agentpulse-manifest.json'),
    `${JSON.stringify({
      manifest_hash: manifest.manifest_hash,
      name: manifest.name,
      role: manifest.role,
      toolsets: executableToolsets,
    }, null, 2)}\n`,
    { encoding: 'utf8', mode: 0o600 },
  );
  await runHermesCommand(runtime, ['--profile', manifest.profile_name, 'config', 'set', 'model', `deepseek/${model}`], env);
  await runHermesCommand(runtime, ['--profile', manifest.profile_name, 'config', 'set', 'terminal.working_dir', workdir], env);
  await runHermesCommand(runtime, ['--profile', manifest.profile_name, 'config', 'set', 'approvals.mode', 'manual'], env);
  if (gatedToolsets.length) {
    await runHermesCommand(runtime, ['--profile', manifest.profile_name, 'tools', 'disable', ...gatedToolsets], env);
  }
  if (executableToolsets.length) {
    await runHermesCommand(runtime, ['--profile', manifest.profile_name, 'tools', 'enable', ...executableToolsets], env);
  }
}

async function syncLocalProfiles(
  runtime: LocalHermesRuntime,
  session: StoredSession,
  device: { id: string; device_token: string; workspace_id: string },
) {
  const bootstrap = await localApiRequest<LocalRuntimeBootstrap>(
    '/local-runtime/bootstrap',
    session.accessToken,
  );
  if (bootstrap.workspace_id !== device.workspace_id) {
    throw new Error('本机 Worker 工作区与当前登录工作区不一致。');
  }
  const syncResults: Array<{
    agent_id: string;
    profile_name: string;
    manifest_hash: string;
    status: 'ready' | 'failed';
    error: string;
  }> = [];
  for (const manifest of bootstrap.profiles) {
    try {
      await materializeLocalProfile(runtime, bootstrap.workspace_id, bootstrap.runtime.model, manifest);
      syncResults.push({
        agent_id: manifest.agent_id,
        profile_name: manifest.profile_name,
        manifest_hash: manifest.manifest_hash,
        status: 'ready',
        error: '',
      });
    } catch (error) {
      syncResults.push({
        agent_id: manifest.agent_id,
        profile_name: manifest.profile_name,
        manifest_hash: manifest.manifest_hash,
        status: 'failed',
        error: redactLocalOutput(
          error instanceof Error ? error.message : '本机 profile 同步失败。',
          localHermesHome(bootstrap.workspace_id),
        ).slice(-500),
      });
    }
  }
  await localApiRequest(
    `/local-devices/${device.id}/profiles/sync`,
    device.device_token,
    { method: 'POST', body: JSON.stringify({ profiles: syncResults }) },
  );
  const failures = syncResults.filter((item) => item.status === 'failed');
  if (failures.length) {
    throw new Error(`${failures.length} 个员工本机 profile 同步失败。`);
  }
  return { workspaceId: bootstrap.workspace_id, profiles: syncResults.length };
}

async function rebindLocalProjects(
  session: StoredSession,
  device: { id: string },
  previous: LocalWorkerState | null,
  workspaceId: string,
): Promise<LocalProject[]> {
  // Device tokens are short-lived. A new desktop process receives a new device
  // identity, but an already owner-authorized directory stays local and can be
  // rebound through the owner's normal session without ever uploading its path.
  if (!previous || previous.workspaceId !== workspaceId) return [];
  const rebound: LocalProject[] = [];
  for (const project of previous.projects) {
    try {
      const root = fs.realpathSync(project.root);
      if (!fs.statSync(root).isDirectory()) continue;
      const registered = await localApiRequest<{ id: string }>(
        '/local-projects',
        session.accessToken,
        {
          method: 'POST',
          body: JSON.stringify({
            device_id: device.id,
            display_name: path.basename(root),
            path_hash: createHash('sha256').update(root).digest('hex'),
            allowed_scopes: project.allowedScopes,
          }),
        },
      );
      rebound.push({ ...project, id: registered.id, root, displayName: path.basename(root) });
    } catch {
      // A moved or removed directory becomes unavailable until its owner picks it again.
    }
  }
  return rebound;
}

async function registerLocalProject(
  session: StoredSession,
  root: string,
  allowedScopes: string[] = ['read'],
) {
  if (!localWorkerState) throw new Error('本机 Worker 未启动');
  const realRoot = fs.realpathSync(root);
  if (!fs.statSync(realRoot).isDirectory()) throw new Error('本机项目不是目录');
  const pathHash = createHash('sha256').update(realRoot).digest('hex');
  const project = await localApiRequest<{ id: string }>(
    '/local-projects',
    session.accessToken,
    {
      method: 'POST',
      body: JSON.stringify({
        device_id: localWorkerState.deviceId,
        display_name: path.basename(realRoot) || realRoot,
        path_hash: pathHash,
        allowed_scopes: allowedScopes,
      }),
    },
  );
  const localProject = {
    id: project.id,
    root: realRoot,
    displayName: path.basename(realRoot) || realRoot,
    allowedScopes,
  };
  localWorkerState.projects = [
    ...localWorkerState.projects.filter(
      (item) => item.id !== project.id && item.root !== realRoot,
    ),
    localProject,
  ];
  writeLocalWorkerState(localWorkerState);
  return { id: project.id, display_name: localProject.displayName, path_hash: pathHash };
}

function isInvalidDeviceToken(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  return /device token|设备 Token|本机 Worker Token|invalid or expired local/i.test(message);
}

async function reconnectLocalWorker() {
  if (localWorkerRestarting) return;
  localWorkerRestarting = true;
  try {
    await startLocalWorker(true);
  } catch (error) {
    if (localWorkerState) {
      localWorkerState.lastError = error instanceof Error ? error.message : String(error);
      try {
        writeLocalWorkerState(localWorkerState);
      } catch {
        // The visible status still exposes the failure if the keychain is unavailable.
      }
    }
  } finally {
    localWorkerRestarting = false;
  }
}

function localProjectForRun(run: Record<string, unknown>) {
  return localWorkerState?.projects.find(
    (project) => project.id === run.local_project_id,
  );
}

function runProcess(command: string, args: string[], cwd: string) {
  return new Promise<string>((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => {
      stdout += String(chunk);
    });
    child.stderr.on('data', (chunk) => {
      stderr += String(chunk);
    });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(stderr.trim() || `${command} exited with ${code}`));
    });
  });
}

async function isolatedProjectForRun(project: LocalProject, runId: string) {
  if (!project.allowedScopes.includes('write')) return project;
  const root = fs.realpathSync(project.root);
  try {
    const insideGit = await runProcess('git', ['rev-parse', '--is-inside-work-tree'], root);
    if (insideGit !== 'true') throw new Error('not a Git worktree');
  } catch (error) {
    throw new Error(
      `并发写入只允许 Git 项目：${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const worktree = path.join(
    app.getPath('userData'),
    'local-worktrees',
    project.id,
    runId,
  );
  if (!fs.existsSync(worktree)) {
    fs.mkdirSync(path.dirname(worktree), { recursive: true });
    await runProcess('git', ['worktree', 'add', '--detach', worktree, 'HEAD'], root);
  }
  return { ...project, root: worktree };
}

function redactLocalOutput(value: string, root: string) {
  return value.replaceAll(root, '[已授权本机项目路径]');
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

type RuntimeEnvelope = {
  workspace_id: string;
  device_id: string;
  run_id: string;
  server_public_key: string;
  nonce: string;
  ciphertext: string;
  expires_at: string;
  mcp_servers?: Array<{
    name: string;
    url: string;
    headers: Record<string, string>;
  }>;
};

function runtimeSessionAad(
  workspaceId: string,
  userId: string,
  deviceId: string,
  runId: string,
  expiresAt: string,
) {
  return JSON.stringify({
    device_id: deviceId,
    expires_at: expiresAt,
    run_id: runId,
    user_id: userId,
    workspace_id: workspaceId,
  });
}

function openRuntimeEnvelope(
  envelope: RuntimeEnvelope,
  privateKey: ReturnType<typeof generateKeyPairSync>['privateKey'],
  userId: string,
) {
  if (Date.parse(envelope.expires_at) <= Date.now()) {
    throw new Error('本机运行凭证已过期。');
  }
  const aad = Buffer.from(
    runtimeSessionAad(
      envelope.workspace_id,
      userId,
      envelope.device_id,
      envelope.run_id,
      envelope.expires_at,
    ),
    'utf8',
  );
  const shared = diffieHellman({
    privateKey,
    publicKey: createPublicKey({
      key: Buffer.from(envelope.server_public_key, 'base64'),
      format: 'der',
      type: 'spki',
    }),
  });
  const key = Buffer.from(
    hkdfSync(
      'sha256',
      shared,
      createHash('sha256').update(aad).digest(),
      Buffer.from('agentpulse/local-runtime/v1'),
      32,
    ),
  );
  const encrypted = Buffer.from(envelope.ciphertext, 'base64');
  if (encrypted.length <= 16) throw new Error('本机运行凭证损坏。');
  const decipher = createDecipheriv(
    'aes-256-gcm',
    key,
    Buffer.from(envelope.nonce, 'base64'),
  );
  decipher.setAAD(aad);
  decipher.setAuthTag(encrypted.subarray(-16));
  const decoded = Buffer.concat([
    decipher.update(encrypted.subarray(0, -16)),
    decipher.final(),
  ]).toString('utf8');
  const value = JSON.parse(decoded) as { DEEPSEEK_API_KEY?: string; model?: string };
  if (!value.DEEPSEEK_API_KEY || !value.model) {
    throw new Error('本机运行凭证内容无效。');
  }
  return {
    DEEPSEEK_API_KEY: value.DEEPSEEK_API_KEY,
    model: value.model,
  };
}

async function requestRuntimeModelEnvironment(runId: string) {
  if (!localWorkerState) throw new Error('本机 Worker 未连接。');
  const session = readStoredSession();
  if (!session) throw new Error('请先登录。');
  const pair = generateKeyPairSync('x25519');
  const clientPublicKey = pair.publicKey
    .export({ format: 'der', type: 'spki' })
    .toString('base64');
  const envelope = await localApiRequest<RuntimeEnvelope>(
    `/local-devices/${localWorkerState.deviceId}/runtime-session`,
    localWorkerState.deviceToken,
    {
      method: 'POST',
      body: JSON.stringify({ run_id: runId, client_public_key: clientPublicKey }),
    },
  );
  if (
    envelope.run_id !== runId ||
    envelope.device_id !== localWorkerState.deviceId ||
    envelope.workspace_id !== localWorkerState.workspaceId
  ) {
    throw new Error('本机运行凭证与当前 Run 不匹配。');
  }
  const modelEnv = openRuntimeEnvelope(envelope, pair.privateKey, session.user.id);
  const mcpServers = Array.isArray(envelope.mcp_servers)
    ? envelope.mcp_servers.filter(
      (server) =>
        typeof server.name === 'string' &&
        typeof server.url === 'string' &&
        (server.url.startsWith('https://') || server.url.startsWith('http://127.0.0.1')) &&
        Boolean(server.headers) &&
        typeof server.headers.Authorization === 'string',
    )
    : [];
  return { modelEnv, mcpServers };
}

function eventDetail(payload: unknown, projectRoot: string) {
  const raw = typeof payload === 'string' ? payload : JSON.stringify(payload ?? {});
  return redactLocalOutput(raw, projectRoot)
    .replaceAll('<｜｜DSML｜｜', '')
    .replaceAll('<|DSML|>', '')
    .slice(0, 10_000);
}

function localAuditValue(value: unknown, projectRoot: string): unknown {
  if (typeof value === 'string') {
    // A project path is useful audit data, but it must be represented as a
    // virtual path before it leaves the desktop. The API never receives the
    // host's absolute root.
    const virtualized = value.replaceAll(projectRoot, 'project://');
    const redacted = redactLocalOutput(virtualized, projectRoot);
    // Never send arbitrary local paths to the API. Project-relative paths are
    // represented by the event title/location; every other absolute path is
    // merely evidence that an external path was referenced.
    if (path.isAbsolute(redacted)) {
      return '[external-local-path]';
    }
    return redacted.slice(0, 2_000);
  }
  if (Array.isArray(value)) {
    return value.slice(0, 50).map((item) => localAuditValue(item, projectRoot));
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .slice(0, 50)
        .map(([key, item]) => [key, localAuditValue(item, projectRoot)]),
    );
  }
  return value;
}

function localAcpToolName(
  payload: Record<string, unknown>,
): 'read_file' | 'write_file' | 'terminal' | 'search_files' | null {
  const title = String(payload.title || '').toLowerCase();
  if (title.startsWith('read:')) return 'read_file';
  if (title.startsWith('write:') || title.startsWith('patch ')) return 'write_file';
  if (title.startsWith('terminal:')) return 'terminal';
  if (title.startsWith('search:')) return 'search_files';
  return null;
}

function localAcpReceiptArguments(payload: Record<string, unknown>, projectRoot: string) {
  return {
    title: localAuditValue(String(payload.title || ''), projectRoot),
    kind: localAuditValue(payload.kind, projectRoot),
    input: localAuditValue(payload.rawInput, projectRoot),
    locations: localAuditValue(payload.locations, projectRoot),
  };
}

function messageChunk(payload: Record<string, unknown>) {
  const content = payload.content;
  if (content && typeof content === 'object' && 'text' in content) {
    return String((content as { text?: unknown }).text ?? '');
  }
  return typeof content === 'string' ? content : '';
}

async function awaitLocalApproval(runId: string, approvalId: string) {
  if (!localWorkerState) return 'deny';
  for (let attempt = 0; attempt < 200; attempt += 1) {
    const approval = await localApiRequest<{ status: string }>(
      `/runs/${runId}/approvals/${approvalId}`,
      localWorkerState.deviceToken,
    );
    if (approval.status === 'approved') return 'allow_once';
    if (approval.status !== 'pending') return 'deny';
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return 'deny';
}

async function executeLocalAcpRun(
  runtime: LocalHermesRuntime,
  run: Record<string, unknown>,
  project: LocalProject,
  modelEnv: { DEEPSEEK_API_KEY: string; model: string },
  mcpServers: Array<{ name: string; url: string; headers: Record<string, string> }>,
) {
  if (!localWorkerState) throw new Error('本机 Worker 未连接。');
  const runId = String(run.id);
  const projectRoot = fs.realpathSync(project.root);
  const runtimeRoot = bundledRuntimeRoot();
  const child = spawn(runtime.python, [runtime.workerScript], {
    cwd: projectRoot,
    env: {
      ...process.env,
      // The Local Worker imports the ACP client itself. It must use the same
      // bundled site-packages as the Hermes launcher in both development and
      // packaged builds; otherwise a staged dev runtime starts Hermes but the
      // sidecar fails before it can create an ACP session.
      PYTHONPATH: [path.join(runtimeRoot, 'site-packages'), process.env.PYTHONPATH]
        .filter(Boolean)
        .join(path.delimiter),
      PYTHONNOUSERSITE: '1',
      NO_COLOR: '1',
    },
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  child.stdin.write(`${JSON.stringify({
    hermes_bin: runtime.command,
    hermes_home: localHermesHome(localWorkerState.workspaceId),
    profile: String(run.hermes_profile_id),
    project_root: projectRoot,
    prompt: String(run.prompt_text || ''),
    resume_session_id: String(run.hermes_session_id || ''),
    model_env: { DEEPSEEK_API_KEY: modelEnv.DEEPSEEK_API_KEY },
    mcp_servers: mcpServers,
  })}\n`);

  let sequence = 2;
  let output = '';
  let workerError = '';
  let lineBuffer = '';
  let pauseCommandSent = false;
  let pauseCompleted = false;
  const receipts = new Map<string, {
    id: string;
    tool: string;
    finished: boolean;
  }>();
  let eventQueue = Promise.resolve();

  const sendEvent = async (
    type: 'message' | 'thinking' | 'tool_call' | 'tool_result' | 'approval_required' | 'status' | 'final',
    detail: string,
    payload: Record<string, unknown> = {},
  ) => {
    const response = await localApiRequest<{ pause_requested?: boolean }>(
      `/runs/${runId}/events`, localWorkerState!.deviceToken, {
      method: 'POST',
      body: JSON.stringify({
        event_seq: sequence++,
        type,
        status: type === 'status' ? 'running' : '',
        title: type === 'status' ? '本机 Hermes' : '',
      detail: eventDetail(detail, projectRoot),
        payload: localAuditValue(payload, projectRoot),
      }),
    });
    return Boolean(response.pause_requested);
  };

  const requestPauseAtSafePoint = (requested: boolean, eventType: string) => {
    if (!requested || pauseCommandSent) return;
    const safeEvent = ['message', 'thinking', 'tool_result', 'status'].includes(eventType);
    const atomicOperationActive = [...receipts.values()].some((receipt) => !receipt.finished);
    if (!safeEvent || atomicOperationActive) return;
    pauseCommandSent = true;
    child.stdin.write(`${JSON.stringify({ type: 'pause' })}\n`);
  };

  const handleWorkerEvent = async (event: Record<string, unknown>) => {
    const eventType = String(event.type || '');
    if (eventType === 'session_started') {
      const pauseRequested = await sendEvent('status', 'Hermes session 已建立', {
        hermes_session_id: String(event.session_id || ''),
        resumed: Boolean(event.resumed),
      });
      requestPauseAtSafePoint(pauseRequested, 'status');
      return;
    }
    if (eventType === 'session_update') {
      const payload = (event.payload ?? {}) as Record<string, unknown>;
      const rawType = String(event.event_type || 'status');
      const allowedEventTypes = new Set<Parameters<typeof sendEvent>[0]>([
        'message', 'thinking', 'tool_call', 'tool_result',
        'approval_required', 'status', 'final',
      ]);
      const type = allowedEventTypes.has(rawType as Parameters<typeof sendEvent>[0])
        ? rawType as Parameters<typeof sendEvent>[0]
        : 'status';
      const toolCallId = String(payload.toolCallId || '');
      const tool = localAcpToolName(payload);
      if (type === 'tool_call' && tool && toolCallId) {
        const receipt = await localApiRequest<{ id: string }>(
          `/runs/${runId}/receipts`,
          localWorkerState!.deviceToken,
          {
            method: 'POST',
            body: JSON.stringify({
              tool_name: tool,
              arguments: localAcpReceiptArguments(payload, projectRoot),
            }),
          },
        );
        receipts.set(toolCallId, { id: receipt.id, tool, finished: false });
      }
      const trackedReceipt = receipts.get(toolCallId);
      if (type === 'tool_result' && trackedReceipt) {
        const status = String(payload.status || 'failed');
        const receiptStatus = status === 'completed' ? 'succeeded'
          : status === 'failed' ? 'failed'
            : null;
        if (receiptStatus) {
          await localApiRequest(
            `/runs/${runId}/receipts/${trackedReceipt.id}`,
            localWorkerState!.deviceToken,
            {
              method: 'POST',
              body: JSON.stringify({
                status: receiptStatus,
                result: {
                  title: localAuditValue(payload.title, projectRoot),
                  locations: localAuditValue(payload.locations, projectRoot),
                  output: localAuditValue(payload.rawOutput, projectRoot),
                },
              }),
            },
          );
          trackedReceipt.finished = true;
        }
      }
      if (type === 'message') output += messageChunk(payload);
      const pauseRequested = await sendEvent(
        type, String(event.update || '本机 Hermes 更新'), payload
      );
      requestPauseAtSafePoint(pauseRequested, type);
      return;
    }
    if (eventType === 'operation_started') {
      const operationId = String(event.operation_id || '');
      const tool = String(event.tool || 'local_tool');
      const argumentsValue = (event.arguments ?? {}) as Record<string, unknown>;
      const receipt = await localApiRequest<{ id: string }>(
        `/runs/${runId}/receipts`,
        localWorkerState!.deviceToken,
        { method: 'POST', body: JSON.stringify({ tool_name: tool, arguments: argumentsValue }) },
      );
      receipts.set(operationId, { id: receipt.id, tool, finished: false });
      await sendEvent('tool_call', tool, { tool, operation_id: operationId });
      return;
    }
    if (eventType === 'operation_finished') {
      const operationId = String(event.operation_id || '');
      const status = String(event.status || 'failed') as 'succeeded' | 'failed' | 'rejected';
      const trackedReceipt = receipts.get(operationId);
      if (trackedReceipt) {
        await localApiRequest(
          `/runs/${runId}/receipts/${trackedReceipt.id}`,
          localWorkerState!.deviceToken,
          {
            method: 'POST',
            body: JSON.stringify({
              status: ['succeeded', 'failed', 'rejected'].includes(status) ? status : 'failed',
              result: (event.result ?? {}) as Record<string, unknown>,
              error: String(event.error || ''),
            }),
          },
        );
        trackedReceipt.finished = true;
      }
      const pauseRequested = await sendEvent(
        'tool_result', String(event.error || event.tool || '本机工具完成'), {
        operation_id: operationId,
        status,
      });
      requestPauseAtSafePoint(pauseRequested, 'tool_result');
      return;
    }
    if (eventType === 'approval_required') {
      const toolCall = (event.tool_call ?? {}) as Record<string, unknown>;
      const approval = await localApiRequest<{ id: string }>(
        `/runs/${runId}/approvals`,
        localWorkerState!.deviceToken,
        {
          method: 'POST',
          body: JSON.stringify({
            tool_name: String(toolCall.title || toolCall.name || 'local_operation'),
            title: String(toolCall.title || '本机操作需确认'),
            description: eventDetail(toolCall, projectRoot),
            arguments: toolCall,
            create_receipt: false,
          }),
        },
      );
      await sendEvent('approval_required', String(toolCall.title || '本机操作等待确认'), {
        approval_id: approval.id,
      });
      const decision = await awaitLocalApproval(runId, approval.id);
      child.stdin.write(`${JSON.stringify({
        type: 'approval_decision',
        approval_id: event.approval_id,
        decision,
      })}\n`);
      return;
    }
    if (eventType === 'error') {
      workerError = String(event.detail || '本机 Hermes 执行失败。');
      return;
    }
    if (eventType === 'paused') {
      pauseCompleted = true;
      await sendEvent('status', '本机 Hermes 已在安全点暂停', {
        hermes_session_id: String(event.session_id || ''),
      });
      return;
    }
    if (eventType === 'final') {
      const fallback = String(event.content || '').trim();
      if (!output && fallback) {
        output = fallback;
        await sendEvent('message', '本机 Hermes 最终回复', {
          content: { type: 'text', text: fallback },
          source: 'acp_frame_fallback',
        });
      }
      await sendEvent('final', '本机 Hermes 已生成最终回复。');
    }
  };

  child.stdout.on('data', (chunk: Buffer) => {
    lineBuffer += chunk.toString('utf8');
    let newline = lineBuffer.indexOf('\n');
    while (newline >= 0) {
      const line = lineBuffer.slice(0, newline).trim();
      lineBuffer = lineBuffer.slice(newline + 1);
      if (line) {
        try {
          const event = JSON.parse(line) as Record<string, unknown>;
          eventQueue = eventQueue.then(() => handleWorkerEvent(event));
        } catch {
          workerError = '本机 Worker 返回了无效事件。';
        }
      }
      newline = lineBuffer.indexOf('\n');
    }
  });
  child.stderr.on('data', (chunk: Buffer) => {
    workerError = `${workerError}\n${chunk.toString('utf8')}`.slice(-2_000);
  });
  const exitCode = await new Promise<number>((resolve) => {
    child.once('error', () => resolve(127));
    child.once('close', (code) => resolve(code ?? 1));
  });
  await eventQueue;
  if (pauseCompleted) {
    return {
      paused: true,
      output: redactLocalOutput(output.trim(), projectRoot),
      lastEventSeq: sequence - 1,
    };
  }
  if (exitCode !== 0 || workerError) {
    throw new Error(redactLocalOutput(workerError || `本机 Worker 退出码 ${exitCode}`, projectRoot));
  }
  const finalOutput = redactLocalOutput(output.trim(), projectRoot);
  if (!finalOutput) throw new Error('本机 Hermes 没有返回最终文本。');
  for (const receipt of receipts.values()) {
    // Hermes v0.18's ACP adapter may omit ToolCallProgress for completed
    // read/search calls. A successful ACP Run after an observed ToolCallStart
    // is enough to close only these side-effect-free operations. Writes and
    // commands stay open unless their explicit completion event arrives.
    if (receipt.finished || !['read_file', 'search_files'].includes(receipt.tool)) continue;
    await localApiRequest(
      `/runs/${runId}/receipts/${receipt.id}`,
      localWorkerState!.deviceToken,
      {
        method: 'POST',
        body: JSON.stringify({
          status: 'succeeded',
          result: {
            source: 'hermes_acp_run_completed',
            detail: 'Observed ACP tool start and successful Run completion.',
          },
        }),
      },
    );
    receipt.finished = true;
  }
  return { paused: false, output: finalOutput, lastEventSeq: sequence - 1 };
}

async function executeLocalRun(run: Record<string, unknown>) {
  if (!localWorkerState) return;
  const authorizedProject = localProjectForRun(run);
  const runId = String(run.id || '');
  if (!runId) return;
  if (!authorizedProject) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({ error: '本机 Worker 找不到 Run 绑定的授权项目。' }),
    });
    return;
  }
  if (!path.isAbsolute(authorizedProject.root) || !fs.existsSync(authorizedProject.root)) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({ error: '本机授权项目目录不存在。' }),
    });
    return;
  }
  let project: LocalProject;
  try {
    project = await isolatedProjectForRun(authorizedProject, runId);
  } catch (error) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({
        error: error instanceof Error ? error.message : '无法建立隔离 Git worktree。',
      }),
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
  let runtime: LocalHermesRuntime;
  try {
    runtime = resolveLocalHermesRuntime();
  } catch (error) {
    await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({
        error: error instanceof Error ? error.message : '内置 Hermes runtime 不可用。',
      }),
    });
    return;
  }
  try {
    await localApiRequest(`/runs/${runId}/lease`, localWorkerState.deviceToken, {
      method: 'POST',
    });
    await postLocalEvent(
      localWorkerState.deviceToken,
      runId,
      1,
      'status',
      'Worker 已接管，正在通过 ACP 在授权项目内执行。',
    );
    const runtimeSession = await requestRuntimeModelEnvironment(runId);
    const result = await executeLocalAcpRun(
      runtime,
      run,
      project,
      runtimeSession.modelEnv,
      runtimeSession.mcpServers,
    );
    if (result.paused) {
      await localApiRequest(
        `/runs/${runId}/pause-complete`,
        localWorkerState.deviceToken,
        {
          method: 'POST',
          body: JSON.stringify({
            checkpoint: { last_event_seq: result.lastEventSeq, output_chars: result.output.length },
          }),
        },
      );
      return;
    }
    const output = result.output;
    await postLocalEvent(localWorkerState.deviceToken, runId, 50_000, 'final', output);
    await localApiRequest(`/runs/${runId}/complete`, localWorkerState.deviceToken, {
      method: 'POST',
      body: JSON.stringify({ message: output, usage: {} }),
    });
  } catch (error) {
    const failure = `本机 Hermes 执行失败：${redactLocalOutput(
      error instanceof Error ? error.message : String(error),
      project.root,
    ).slice(-2_000)}`;
    try {
      await localApiRequest(`/runs/${runId}/fail`, localWorkerState.deviceToken, {
        method: 'POST',
        body: JSON.stringify({ error: failure }),
      });
    } catch (failError) {
      if (localWorkerState) {
        localWorkerState.lastError = `${failure}\n无法回写失败状态：${
          failError instanceof Error ? failError.message : String(failError)
        }`;
        writeLocalWorkerState(localWorkerState);
      }
    }
  }
}

async function pollLocalRuns() {
  if (localWorkerPolling || !localWorkerState) return;
  const slots = localWorkerMaxRuns - localWorkerRuns.size;
  if (slots <= 0) return;
  localWorkerPolling = true;
  try {
    const resources = [
      ...localWorkerState.projects.map((project) => ({
        resource_type: 'local_file',
        resource_key: project.id,
        mode: 'shared',
      })),
      ...Array.from({ length: localWorkerMaxRuns }, (_, index) => ({
        resource_type: 'local_terminal',
        resource_key: `terminal:${index}`,
        mode: 'exclusive',
      })),
    ];
    const payload = await localApiRequest<{ runs: Array<Record<string, unknown>> }>(
      `/local-devices/${localWorkerState.deviceId}/runs/claim`,
      localWorkerState.deviceToken,
      {
        method: 'POST',
        body: JSON.stringify({ max_runs: slots, available_resources: resources }),
      },
    );
    for (const run of payload.runs) {
      const runId = String(run.id || '');
      if (!runId || localWorkerRuns.has(runId)) continue;
      localWorkerRuns.set(runId, run);
      void executeLocalRun(run).finally(() => {
        localWorkerRuns.delete(runId);
        void pollLocalRuns();
      });
    }
  } catch (error) {
    localWorkerState.lastError = error instanceof Error ? error.message : String(error);
    try {
      writeLocalWorkerState(localWorkerState);
    } catch {
      // Keep the worker alive; status IPC exposes the last error.
    }
    if (isInvalidDeviceToken(error)) void reconnectLocalWorker();
  } finally {
    localWorkerPolling = false;
  }
}

async function startLocalWorkerInternal(force = false) {
  if (
    !force &&
    localWorkerState &&
    !localWorkerState.lastError &&
    localWorkerTimer &&
    localHeartbeatTimer
  ) {
    return {
      online: true,
      deviceId: localWorkerState.deviceId,
      hermes: resolveLocalHermesRuntime().label,
    };
  }
  const session = readStoredSession();
  if (!session) return { online: false, reason: '未登录' };
  let runtime: LocalHermesRuntime;
  try {
    runtime = resolveLocalHermesRuntime();
  } catch (error) {
    return {
      online: false,
      reason: error instanceof Error ? error.message : '内置 Hermes runtime 不可用。',
    };
  }
  const previous = readLocalWorkerState();
  const device = await localApiRequest<{ id: string; device_token: string; workspace_id: string }>(
    '/local-devices/register',
    session.accessToken,
    {
      method: 'POST',
      body: JSON.stringify({
        device_name: `${app.getName()} 本机`,
        replaces_device_id: previous?.deviceId ?? null,
        platform: process.platform,
        architecture: process.arch,
        worker_version: app.getVersion(),
        hermes_version: '0.18.2',
        capabilities: {
          read_file: true,
          write_file: true,
          terminal: true,
          browser: false,
          computer_use: false,
        },
      }),
    },
  );
  const profileSync = await syncLocalProfiles(runtime, session, device);
  const projects = await rebindLocalProjects(
    session,
    device,
    previous,
    profileSync.workspaceId,
  );
  localWorkerState = {
    deviceId: device.id,
    deviceToken: device.device_token,
    workspaceId: profileSync.workspaceId,
    projects,
    profilesReady: true,
    lastError: '',
  };
  writeLocalWorkerState(localWorkerState);
  if (localWorkerTimer) clearInterval(localWorkerTimer);
  if (localHeartbeatTimer) clearInterval(localHeartbeatTimer);
  localWorkerTimer = setInterval(() => void pollLocalRuns(), 2000);
  localHeartbeatTimer = setInterval(async () => {
    // The server rotates the short-lived device token on heartbeat. Never
    // rotate while an ACP Run is issuing its own authenticated tool events.
    if (!localWorkerState || localWorkerPolling || localWorkerRuns.size > 0) return;
    try {
      const heartbeat = await localApiRequest<{ device_token: string }>(
        `/local-devices/${localWorkerState.deviceId}/heartbeat`,
        localWorkerState.deviceToken,
        { method: 'POST', body: JSON.stringify({ hermes_version: '0.18.2' }) },
      );
      localWorkerState.deviceToken = heartbeat.device_token;
      localWorkerState.lastError = '';
      writeLocalWorkerState(localWorkerState);
    } catch (error) {
      localWorkerState.lastError = error instanceof Error ? error.message : String(error);
      if (isInvalidDeviceToken(error)) void reconnectLocalWorker();
    }
  }, 15_000);
  void pollLocalRuns();
  return {
    online: true,
    deviceId: device.id,
    hermes: runtime.label,
    profiles: profileSync.profiles,
  };
}

async function startLocalWorker(force = false) {
  if (localWorkerStartPromise) return localWorkerStartPromise;
  localWorkerStartPromise = startLocalWorkerInternal(force);
  try {
    return await localWorkerStartPromise;
  } finally {
    localWorkerStartPromise = null;
  }
}

function registerLocalRuntimeIpc() {
  ipcMain.handle('agentpulse:local-runtime:start', () => startLocalWorker());
  ipcMain.handle('agentpulse:local-runtime:status', () => {
    let hermes = '内置 runtime 未就绪';
    let runtimeError = '';
    try {
      hermes = resolveLocalHermesRuntime().label;
    } catch (error) {
      runtimeError = error instanceof Error ? error.message : '内置 Hermes runtime 不可用。';
    }
    return {
      online: Boolean(localWorkerState) && !runtimeError && !localWorkerState?.lastError,
      deviceId: localWorkerState?.deviceId ?? null,
      hermes,
      projects: localWorkerState?.projects.map(({ root: _root, ...project }) => project) ?? [],
      profilesReady: Boolean(localWorkerState?.profilesReady),
      lastError: runtimeError || localWorkerState?.lastError || '',
    };
  });
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
    return registerLocalProject(session, root);
  });
  ipcMain.handle('agentpulse:local-project:authorize-message', async (_event, text: string) => {
    if (typeof text !== 'string' || text.length > 50_000) return null;
    const root = resolveExplicitLocalProjectRoot(text);
    if (!root) return null;
    const session = readStoredSession();
    if (!session) throw new Error('请先登录');
    await startLocalWorker();
    return registerLocalProject(session, root);
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
  if (isDev) {
    mainWindow.webContents.on('console-message', (_event, level, message, line, sourceId) => {
      console.error(`Renderer console [${level}] ${sourceId}:${line} ${message}`);
    });
  }

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
