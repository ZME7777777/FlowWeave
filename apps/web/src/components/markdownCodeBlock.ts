import type { ReactNode } from 'react';

interface MarkdownFence {
  indent: string;
  marker: string;
  info: string;
}

function markdownFence(line: string): MarkdownFence | undefined {
  const match = line.match(/^([ \t]*)(`{3,}|~{3,})([^\s`~]*)(?:[ \t]+.*)?$/);
  return match ? { indent: match[1], marker: match[2], info: match[3] } : undefined;
}

function replaceMarkdownFence(line: string, marker: string): string {
  const fence = markdownFence(line);
  return fence ? `${fence.indent}${marker}${line.slice(fence.indent.length + fence.marker.length)}` : line;
}

/** Protect Markdown source wrappers whose inner fence reuses the same delimiter. */
export function normalizeNestedMarkdownFences(markdown: string): string {
  const newline = markdown.includes('\r\n') ? '\r\n' : '\n';
  const trailingNewline = /(?:\r\n|\n)$/.test(markdown);
  const lines = markdown.split(/\r\n|\n/);
  if (trailingNewline) lines.pop();

  for (let openingIndex = 0; openingIndex < lines.length; openingIndex += 1) {
    const opening = markdownFence(lines[openingIndex]);
    if (!opening || !['md', 'markdown'].includes(opening.info.toLowerCase())) continue;

    let nested = false;
    let nestedFound = false;
    let outerCloseIndex: number | undefined;
    let longestNestedMarker = opening.marker.length;
    for (let index = openingIndex + 1; index < lines.length; index += 1) {
      const candidate = markdownFence(lines[index]);
      if (!candidate || candidate.marker[0] !== opening.marker[0] || candidate.marker.length < opening.marker.length) continue;
      if (candidate.info) {
        nested = true;
        nestedFound = true;
        longestNestedMarker = Math.max(longestNestedMarker, candidate.marker.length);
      } else if (nested) {
        nested = false;
      } else {
        outerCloseIndex = index;
        break;
      }
    }
    if (!nestedFound) continue;

    const safeMarker = opening.marker[0].repeat(Math.max(opening.marker.length, longestNestedMarker + 1));
    lines[openingIndex] = replaceMarkdownFence(lines[openingIndex], safeMarker);
    if (outerCloseIndex === undefined) {
      lines.push(safeMarker);
      openingIndex = lines.length - 1;
    } else {
      lines[outerCloseIndex] = replaceMarkdownFence(lines[outerCloseIndex], safeMarker);
      openingIndex = outerCloseIndex;
    }
  }

  return lines.join(newline) + (trailingNewline ? newline : '');
}

export function markdownCodeText(children: ReactNode): string {
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(markdownCodeText).join('');
  return '';
}

export function isMermaidDiagram(className: string | undefined, source: string): boolean {
  return /(?:^|\s)language-mermaid(?:\s|$)/.test(className ?? '')
    || /^(?:sequenceDiagram|flowchart|graph|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline|gitGraph|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic)\b/m.test(source.trim());
}
