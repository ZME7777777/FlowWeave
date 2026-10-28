import hljs from 'highlight.js/lib/common';

export function filePreviewLanguage(path: string): string | undefined {
  const extension = path.split('.').pop()?.toLowerCase();
  switch (extension) {
    case 'py': case 'pyi': return 'python';
    case 'java': return 'java';
    case 'sql': return 'sql';
    case 'ts': case 'tsx': return 'typescript';
    case 'js': case 'jsx': case 'mjs': case 'cjs': return 'javascript';
    case 'json': return 'json';
    case 'yaml': case 'yml': return 'yaml';
    case 'xml': case 'svg': case 'html': case 'htm': case 'vue': return 'xml';
    case 'css': case 'scss': case 'less': return 'css';
    case 'sh': case 'bash': case 'zsh': return 'bash';
    case 'go': return 'go';
    case 'rs': return 'rust';
    case 'rb': return 'ruby';
    case 'php': return 'php';
    case 'c': case 'h': return 'c';
    case 'cc': case 'cpp': case 'cxx': case 'hpp': return 'cpp';
    case 'cs': return 'csharp';
    case 'kt': case 'kts': return 'kotlin';
    case 'toml': case 'ini': case 'conf': case 'properties': return 'ini';
    default: return undefined;
  }
}

export function highlightedCode(value: string, language?: string): string {
  if (language && hljs.getLanguage(language)) return hljs.highlight(value, { language, ignoreIllegals: true }).value;
  return hljs.highlightAuto(value).value;
}


export type SplitDiffRow<T> = {
  before?: { line: T; index: number };
  after?: { line: T; index: number };
};

/**
 * Align each contiguous replacement block before handing it to the two panes.
 * A unified diff serializes removals and additions, whereas a split diff must
 * show them on the same visual rows.
 */
export function splitDiffRows<T extends { kind: 'context' | 'addition' | 'deletion' }>(
  lines: T[],
  replacementBoundary: (previous: T, next: T) => boolean = () => false,
): SplitDiffRow<T>[] {
  const rows: SplitDiffRow<T>[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (line.kind === 'context') {
      rows.push({ before: { line, index }, after: { line, index } });
      index += 1;
      continue;
    }
    const deletions: Array<{ line: T; index: number }> = [];
    const additions: Array<{ line: T; index: number }> = [];
    const blockStart = index;
    while (index < lines.length && lines[index].kind !== 'context'
      && (index === blockStart || !replacementBoundary(lines[index - 1], lines[index]))) {
      const changed = { line: lines[index], index };
      if (changed.line.kind === 'deletion') deletions.push(changed);
      else additions.push(changed);
      index += 1;
    }
    for (let offset = 0; offset < Math.max(deletions.length, additions.length); offset += 1) {
      rows.push({ before: deletions[offset], after: additions[offset] });
    }
  }
  return rows;
}


export function diffChangeStartIndexes<T extends { kind: 'context' | 'addition' | 'deletion' }>(lines: T[]): number[] {
  return lines.flatMap((line, index) => line.kind !== 'context' && (index === 0 || lines[index - 1].kind === 'context') ? [index] : []);
}


export type GitDiffLine = { oldLine?: number; newLine?: number; kind: 'context' | 'addition' | 'deletion'; text: string; hunk: number };

export function gitDiffLines(value: string): GitDiffLine[] {
  const lines: GitDiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  let hunkNumber = 0;
  let inHunk = false;
  for (const sourceLine of value.split('\n')) {
    const hunkMatch = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(sourceLine);
    if (hunkMatch) {
      oldLine = Number(hunkMatch[1]);
      newLine = Number(hunkMatch[2]);
      hunkNumber += 1;
      inHunk = true;
      continue;
    }
    if (!inHunk || sourceLine === '\\ No newline at end of file') continue;
    if (sourceLine.startsWith('+')) {
      lines.push({ kind: 'addition', newLine, text: sourceLine.slice(1), hunk: hunkNumber });
      newLine += 1;
    } else if (sourceLine.startsWith('-')) {
      lines.push({ kind: 'deletion', oldLine, text: sourceLine.slice(1), hunk: hunkNumber });
      oldLine += 1;
    } else if (sourceLine.startsWith(' ')) {
      lines.push({ kind: 'context', oldLine, newLine, text: sourceLine.slice(1), hunk: hunkNumber });
      oldLine += 1;
      newLine += 1;
    }
  }
  return lines;
}
