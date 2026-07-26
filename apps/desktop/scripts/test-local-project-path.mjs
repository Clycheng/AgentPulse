import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { resolveExplicitLocalProjectRoot } from '../dist-electron/local-project-path.js';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'agentpulse-local-path-'));
const spaced = path.join(root, 'project with spaces');
fs.mkdirSync(spaced);
fs.writeFileSync(path.join(root, 'README.md'), 'AgentPulse', 'utf8');

try {
  const realRoot = fs.realpathSync(root);
  const realSpaced = fs.realpathSync(spaced);
  assert.equal(resolveExplicitLocalProjectRoot(`${root} 看下这个项目`), realRoot);
  assert.equal(resolveExplicitLocalProjectRoot(`请读取 "${spaced}"`), realSpaced);
  assert.equal(
    resolveExplicitLocalProjectRoot(`${path.join(root, 'README.md')} 看下文件`),
    realRoot,
  );
  assert.equal(resolveExplicitLocalProjectRoot('/missing/agentpulse/project 看下项目'), null);
  assert.equal(resolveExplicitLocalProjectRoot('打开 https://agentpulse.cc'), null);
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}

console.log('local project path tests passed');
