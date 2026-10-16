import { Archive, Braces, FileAudio, FileCode2, FileSpreadsheet, FileText, FileType2, FileVideo, Presentation, type LucideIcon } from 'lucide-react';
import './file-type-icon.css';

interface FileTypeIconProps {
  filename: string;
  mimeType?: string;
  size?: number;
  className?: string;
}

type FileKind = 'document' | 'spreadsheet' | 'presentation' | 'code' | 'data' | 'archive' | 'audio' | 'video' | 'generic';

const EXTENSIONS: Record<FileKind, Set<string>> = {
  document: new Set(['doc', 'docx', 'odt', 'pdf', 'rtf', 'txt', 'md', 'mdx', 'pages']),
  spreadsheet: new Set(['csv', 'tsv', 'xls', 'xlsx', 'ods', 'numbers']),
  presentation: new Set(['ppt', 'pptx', 'odp', 'key']),
  code: new Set(['c', 'cc', 'cpp', 'cs', 'css', 'go', 'h', 'hpp', 'html', 'java', 'js', 'jsx', 'kt', 'php', 'py', 'rb', 'rs', 'sh', 'sql', 'swift', 'ts', 'tsx', 'vue', 'xml', 'yaml', 'yml']),
  data: new Set(['json', 'toml', 'ini', 'cfg', 'conf', 'env', 'log']),
  archive: new Set(['7z', 'bz2', 'gz', 'rar', 'tar', 'xz', 'zip']),
  audio: new Set(['aac', 'flac', 'm4a', 'mp3', 'ogg', 'wav', 'wma']),
  video: new Set(['avi', 'mkv', 'mov', 'mp4', 'mpeg', 'mpg', 'webm']),
  generic: new Set(),
};

function fileKind(filename: string, mimeType?: string): FileKind {
  const extension = filename.toLowerCase().split('.').at(-1) ?? '';
  const fromExtension = (Object.keys(EXTENSIONS) as FileKind[]).find(kind => EXTENSIONS[kind].has(extension));
  if (fromExtension) return fromExtension;

  const normalizedMime = mimeType?.toLowerCase() ?? '';
  if (normalizedMime.startsWith('audio/')) return 'audio';
  if (normalizedMime.startsWith('video/')) return 'video';
  if (normalizedMime.includes('spreadsheet') || normalizedMime.includes('excel') || normalizedMime === 'text/csv') return 'spreadsheet';
  if (normalizedMime.includes('presentation') || normalizedMime.includes('powerpoint')) return 'presentation';
  if (normalizedMime.includes('pdf') || normalizedMime.startsWith('text/')) return 'document';
  if (normalizedMime.includes('zip') || normalizedMime.includes('compressed') || normalizedMime.includes('archive')) return 'archive';
  if (normalizedMime.includes('json') || normalizedMime.includes('yaml') || normalizedMime.includes('xml')) return 'data';
  if (normalizedMime.includes('javascript') || normalizedMime.includes('typescript') || normalizedMime.includes('python') || normalizedMime.includes('source')) return 'code';
  return 'generic';
}

const ICONS: Record<FileKind, LucideIcon> = {
  document: FileText,
  spreadsheet: FileSpreadsheet,
  presentation: Presentation,
  code: FileCode2,
  data: Braces,
  archive: Archive,
  audio: FileAudio,
  video: FileVideo,
  generic: FileType2,
};

export function FileTypeIcon({ filename, mimeType, size = 16, className }: FileTypeIconProps) {
  const kind = fileKind(filename, mimeType);
  const Icon = ICONS[kind];
  return <span className={`file-type-icon ${kind}${className ? ` ${className}` : ''}`} aria-label={`${kind} file`}><Icon size={size}/></span>;
}
