import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
const runtimeRoot = path.join(desktopRoot, 'runtime-stage');
const manifestPath = path.join(runtimeRoot, 'runtime.json');

if (!fs.existsSync(manifestPath)) {
  throw new Error('Missing generated Hermes runtime. Run npm run prepare:runtime first.');
}
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
if (manifest.platform !== process.platform || manifest.architecture !== process.arch) {
  throw new Error(`Runtime target mismatch: ${manifest.platform}/${manifest.architecture}`);
}
if (manifest.hermes?.version !== '0.18.2') {
  throw new Error('Unexpected Hermes version in runtime manifest');
}
const launcher = path.join(runtimeRoot, manifest.launcher);
if (!fs.existsSync(launcher)) throw new Error('Runtime launcher is missing');
const mainPath = path.join(runtimeRoot, 'site-packages', 'hermes_cli', 'main.py');
if (!fs.existsSync(mainPath)) throw new Error('Hermes runtime entrypoint is missing');
const actualHash = createHash('sha256').update(fs.readFileSync(mainPath)).digest('hex');
if (!manifest.main_sha256 || actualHash !== manifest.main_sha256) {
  throw new Error('Hermes runtime integrity check failed');
}
const workerPath = path.join(runtimeRoot, 'local-worker', 'agentpulse_local_worker.py');
if (!fs.existsSync(workerPath)) throw new Error('Local Worker is missing');
const workerHash = createHash('sha256').update(fs.readFileSync(workerPath)).digest('hex');
if (!manifest.local_worker_sha256 || workerHash !== manifest.local_worker_sha256) {
  throw new Error('Local Worker integrity check failed');
}
const version = execFileSync(launcher, ['--version'], { encoding: 'utf8' });
if (!version.includes('Hermes Agent v0.18.2')) {
  throw new Error(`Unexpected runtime version: ${version.trim()}`);
}
const python = path.join(
  runtimeRoot,
  'python',
  process.platform === 'win32' ? 'python.exe' : 'bin/python3.11',
);
if (!fs.existsSync(python)) throw new Error('Bundled Local Worker Python is missing');
execFileSync(python, ['-c', 'import acp; import hermes_cli.main'], {
  encoding: 'utf8',
  env: {
    ...process.env,
    PYTHONPATH: path.join(runtimeRoot, 'site-packages'),
    PYTHONNOUSERSITE: '1',
  },
});
console.log(`Verified bundled Hermes runtime: ${version.trim()}`);
