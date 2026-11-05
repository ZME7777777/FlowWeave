import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { isMermaidDiagram, normalizeNestedMarkdownFences } from './markdownCodeBlock';

// Keep the plain-text fast path shared with ConversationMarkdown: punctuation
// which is rendered literally must also remain searchable literally.
export const CONVERSATION_MARKDOWN_SYNTAX = /(^|\n)\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```)|[`*_~$]|!?\[[^\]]*\]\([^)]*\)|\||https?:\/\/[^\s<]+/i;

const parser = unified().use(remarkParse).use(remarkGfm).use(remarkMath);
interface SearchMarkdownNode {
  type: string;
  value?: string;
  lang?: string | null;
  children?: SearchMarkdownNode[];
}

export function conversationSearchPattern(query: string): RegExp {
  return new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
}

export function conversationSearchText(content: string): string {
  const markdown = normalizeNestedMarkdownFences(content);
  if (!CONVERSATION_MARKDOWN_SYNTAX.test(markdown)) return markdown;
  const text = (node: SearchMarkdownNode): string => {
    if (['image', 'imageReference', 'definition', 'html', 'math', 'inlineMath'].includes(node.type)) return '';
    if (node.type === 'code' && isMermaidDiagram(node.lang ? `language-${node.lang}` : undefined, node.value ?? '')) return '';
    if (node.value !== undefined) return node.value;
    if (node.type === 'break') return '\n';
    const separator = ['root', 'blockquote', 'list', 'listItem', 'table', 'tableRow'].includes(node.type) ? '\n' : '';
    return node.children?.map(text).join(separator) ?? '';
  };
  // The rendered label is searchable; Markdown delimiters and link destinations
  // are not text occurrences and must not create unreachable navigation results.
  return text(parser.parse(markdown));
}
