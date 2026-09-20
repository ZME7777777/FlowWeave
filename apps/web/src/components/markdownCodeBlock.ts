import type { ReactNode } from 'react';

export function markdownCodeText(children: ReactNode): string {
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(markdownCodeText).join('');
  return '';
}

export function isMermaidDiagram(className: string | undefined, source: string): boolean {
  return /(?:^|\s)language-mermaid(?:\s|$)/.test(className ?? '')
    || /^(?:sequenceDiagram|flowchart|graph|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline|gitGraph|quadrantChart|requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic)\b/m.test(source.trim());
}
