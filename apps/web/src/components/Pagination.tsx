interface Props {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
}

export function Pagination({ page, pageSize, total, onPageChange }: Props) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  return <nav className="pagination" aria-label="列表分页">
    <span>共 {total} 项，第 {page} / {pageCount} 页</span>
    <div><button className="secondary" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>上一页</button><button className="secondary" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>下一页</button></div>
  </nav>;
}
