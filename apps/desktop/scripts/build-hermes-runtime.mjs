import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const lockPath = path.join(desktopRoot, 'runtime', 'hermes-runtime.lock.json');
const stageRoot = path.join(desktopRoot, 'runtime-stage');
const cacheRoot = path.join(desktopRoot, '.runtime-cache');
const workerSource = path.join(desktopRoot, 'local-worker', 'agentpulse_local_worker.py');
const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));

function option(name) {
  const prefix = `--${name}=`;
  const value = process.argv.find((arg) => arg.startsWith(prefix));
  return value ? value.slice(prefix.length) : null;
}

const targetPlatform = option('platform') ?? process.platform;
const targetArch = option('arch') ?? process.arch;
const expected = { darwin: 'arm64', win32: 'x64' };

if (!Object.hasOwn(expected, targetPlatform) || expected[targetPlatform] !== targetArch) {
  throw new Error(`Unsupported bundled runtime target: ${targetPlatform}/${targetArch}`);
}
if (targetPlatform !== process.platform || targetArch !== process.arch) {
  throw new Error(
    `Runtime must be built on its target platform: requested ${targetPlatform}/${targetArch}, running ${process.platform}/${process.arch}`,
  );
}

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    cwd: desktopRoot,
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf8',
    ...options,
  });
}

function ensureSource() {
  const source = path.join(cacheRoot, 'hermes-agent');
  const headPath = path.join(source, '.git', 'HEAD');
  if (!fs.existsSync(headPath)) {
    fs.rmSync(source, { recursive: true, force: true });
    fs.mkdirSync(cacheRoot, { recursive: true });
    run('git', ['clone', '--no-checkout', lock.hermes.repository, source]);
  }
  run('git', ['-C', source, 'fetch', '--depth', '1', 'origin', lock.hermes.commit]);
  run('git', ['-C', source, 'checkout', '--detach', '--force', lock.hermes.commit]);
  const resolved = run('git', ['-C', source, 'rev-parse', 'HEAD']).trim();
  if (resolved !== lock.hermes.commit) {
    throw new Error(`Hermes source mismatch: expected ${lock.hermes.commit}, got ${resolved}`);
  }
  return source;
}

function resolvePython() {
  run('uv', ['python', 'install', lock.python.version], { stdio: 'inherit' });
  const python = run('uv', ['python', 'find', lock.python.version]).trim();
  if (!python || !fs.existsSync(python)) {
    throw new Error('uv did not provide a Python 3.11 runtime');
  }
  return python;
}

function pythonRoot(python) {
  return process.platform === 'win32'
    ? path.dirname(python)
    : path.dirname(path.dirname(python));
}

function sha256(file) {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function writeLauncher(python) {
  const binRoot = path.join(stageRoot, 'bin');
  fs.mkdirSync(binRoot, { recursive: true });
  if (process.platform === 'win32') {
    const launcher = path.join(binRoot, 'hermes.cmd');
    fs.writeFileSync(
      launcher,
      [
        '@echo off',
        'setlocal',
        'set RUNTIME_ROOT=%~dp0..',
        'set PYTHONPATH=%RUNTIME_ROOT%\\site-packages',
        'set PYTHONNOUSERSITE=1',
        '"%RUNTIME_ROOT%\\python\\python.exe" -m hermes_cli.main %*',
      ].join('\r\n'),
    );
    return launcher;
  }
  const launcher = path.join(binRoot, 'hermes');
  fs.writeFileSync(
    launcher,
    [
      '#!/bin/sh',
      'set -eu',
      'RUNTIME_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"',
      'export PYTHONPATH="$RUNTIME_ROOT/site-packages"',
      'export PYTHONNOUSERSITE=1',
      `exec "$RUNTIME_ROOT/python/bin/${path.basename(python)}" -m hermes_cli.main "$@"`,
    ].join('\n'),
    { mode: 0o755 },
  );
  return launcher;
}

const source = ensureSource();
const hostPython = resolvePython();
const hostPythonRoot = pythonRoot(hostPython);
fs.rmSync(stageRoot, { recursive: true, force: true });
fs.mkdirSync(stageRoot, { recursive: true });

const stagedPythonRoot = path.join(stageRoot, 'python');
fs.cpSync(hostPythonRoot, stagedPythonRoot, { recursive: true, dereference: true });
const stagedPython = process.platform === 'win32'
  ? path.join(stagedPythonRoot, 'python.exe')
  : path.join(stagedPythonRoot, 'bin', path.basename(hostPython));
if (!fs.existsSync(stagedPython)) {
  throw new Error(`Staged Python was not found at ${stagedPython}`);
}

const sitePackages = path.join(stageRoot, 'site-packages');
fs.mkdirSync(sitePackages, { recursive: true });
run('uv', [
  'pip', 'install', '--python', stagedPython, '--target', sitePackages,
  `${source}[acp,mcp,computer-use]`,
], { stdio: 'inherit' });

const launcher = writeLauncher(stagedPython);
if (!fs.existsSync(workerSource)) {
  throw new Error('Missing Local Worker source');
}
const workerPath = path.join(stageRoot, 'local-worker', 'agentpulse_local_worker.py');
fs.mkdirSync(path.dirname(workerPath), { recursive: true });
fs.copyFileSync(workerSource, workerPath);
const manifest = {
  format: 1,
  platform: targetPlatform,
  architecture: targetArch,
  hermes: lock.hermes,
  python: lock.python.version,
  launcher: path.relative(stageRoot, launcher).split(path.sep).join('/'),
  main_sha256: sha256(path.join(sitePackages, 'hermes_cli', 'main.py')),
  local_worker_sha256: sha256(workerPath),
  built_at: new Date().toISOString(),
  build_host: `${os.platform()}/${os.arch()}`,
};
fs.writeFileSync(path.join(stageRoot, 'runtime.json'), `${JSON.stringify(manifest, null, 2)}\n`);

run(launcher, ['--version'], {
  cwd: stageRoot,
  env: { ...process.env, HOME: fs.mkdtempSync(path.join(os.tmpdir(), 'agentpulse-runtime-home-')) },
});
console.log(`Built Hermes runtime at ${stageRoot}`);
