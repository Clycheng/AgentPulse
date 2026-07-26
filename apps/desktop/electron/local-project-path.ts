import fs from 'node:fs';
import path from 'node:path';

function candidatePaths(text: string): string[] {
  const candidates: string[] = [];
  const quoted = /(["'`])((?:\/[^"]+?|[A-Za-z]:\\[^"']+?))\1/g;
  const unix = /(?:^|\s)(\/[^\s"'`<>|]+)/g;
  const windows = /(?:^|\s)([A-Za-z]:\\[^\s"'`<>|]+)/g;

  for (const match of text.matchAll(quoted)) candidates.push(match[2]);
  for (const match of text.matchAll(unix)) candidates.push(match[1]);
  for (const match of text.matchAll(windows)) candidates.push(match[1]);
  return candidates;
}

export function resolveExplicitLocalProjectRoot(text: string): string | null {
  for (const rawCandidate of candidatePaths(text)) {
    const candidate = rawCandidate.replace(/[，。；;!?！？)\]}]+$/u, '');
    if (!path.isAbsolute(candidate)) continue;
    try {
      const realPath = fs.realpathSync(candidate);
      const stat = fs.statSync(realPath);
      if (stat.isDirectory()) return realPath;
      if (stat.isFile()) return path.dirname(realPath);
    } catch {
      // Invalid or missing paths are not authorization.
    }
  }
  return null;
}
