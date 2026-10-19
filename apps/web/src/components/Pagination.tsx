import { ChevronLeft, ChevronRight } from 'lucide-react';

interface Props {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  variant?: 'default' | 'arrows';
}

export function Pagination({ page, pageSize, total, onPageChange, variant = 'default' }: Props) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  const arrows = variant === 'arrows';
  return <nav className={`pagination${arrows ? ' pagination-arrows' : ''}`} aria-label="列表分页">
    {arrows ? <span className="pagination-page-status">共 {total} 项，第 {page} / {pageCount} 页</span> : <span>共 {total} 项，第 {page} / {pageCount} 页</span>}
    {arrows ? <div className="pagination-arrow-actions"><button className="pagination-arrow" aria-label="上一页" title="上一页" disabled={page <= 1} onClick={() => onPageChange(page - 1)}><ChevronLeft size={16}/></button><button className="pagination-arrow" aria-label="下一页" title="下一页" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}><ChevronRight size={16}/></button></div> : <div><button className="secondary" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>上一页</button><button className="secondary" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>下一页</button></div>}
  </nav>;
}
