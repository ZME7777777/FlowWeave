import { AlertTriangle, FolderPlus, HelpCircle, X } from 'lucide-react';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { useEscapeClose } from './useEscapeClose';
import { DialogContext, type ConfirmOptions, type DialogApi, type PromptOptions } from './ProductDialogContext';

type DialogRequest =
  | ({ kind: 'confirm' } & ConfirmOptions)
  | ({ kind: 'prompt' } & PromptOptions);
type DialogResult = boolean | string | null;
export function ProductDialogProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<DialogRequest>();
  const [value, setValue] = useState('');
  const resolver = useRef<((result: DialogResult) => void) | null>(null);

  const settle = (result: DialogResult) => {
    resolver.current?.(result);
    resolver.current = null;
    setRequest(undefined);
    setValue('');
  };
  useEscapeClose(() => settle(request?.kind === 'confirm' ? false : null), Boolean(request));

  const api = useMemo<DialogApi>(() => ({
    confirm: options => new Promise<boolean>(resolve => {
      resolver.current = resolve as (result: DialogResult) => void;
      setRequest({ kind: 'confirm', ...options });
    }),
    prompt: options => new Promise<string | null>(resolve => {
      resolver.current = resolve as (result: DialogResult) => void;
      setValue(options.initialValue ?? '');
      setRequest({ kind: 'prompt', ...options });
    }),
  }), []);

  const confirm = () => {
    if (!request) return;
    if (request.kind === 'prompt') {
      const normalized = value.trim();
      if (!normalized) return;
      settle(normalized);
    } else settle(true);
  };

  return <DialogContext.Provider value={api}>{children}{request && <div className="modal-backdrop product-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) settle(request.kind === 'confirm' ? false : null); }}>
    <section className={`modal product-dialog ${request.tone === 'danger' ? 'danger-dialog' : ''}`} role="alertdialog" aria-modal="true" aria-labelledby="product-dialog-title" aria-describedby="product-dialog-message">
      <header><span className="product-dialog-icon">{request.kind === 'prompt' ? <FolderPlus size={20}/> : request.tone === 'danger' ? <AlertTriangle size={20}/> : <HelpCircle size={20}/>}</span><div><span className="eyebrow">{request.kind === 'prompt' ? 'INPUT REQUIRED' : 'PLEASE CONFIRM'}</span><h2 id="product-dialog-title">{request.title}</h2></div><button type="button" className="ghost product-dialog-close" aria-label="关闭对话框" onClick={() => settle(request.kind === 'confirm' ? false : null)}><X size={17}/></button></header>
      <p id="product-dialog-message">{request.message}</p>
      {request.kind === 'prompt' && <label>{request.inputLabel}<input autoFocus value={value} placeholder={request.placeholder} onChange={event => setValue(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); confirm(); } }}/></label>}
      <footer><button type="button" className="secondary" onClick={() => settle(request.kind === 'confirm' ? false : null)}>{request.cancelLabel ?? '取消'}</button><button type="button" className={request.tone === 'danger' ? 'danger product-dialog-confirm' : 'primary'} disabled={request.kind === 'prompt' && !value.trim()} onClick={confirm}>{request.confirmLabel ?? '确认'}</button></footer>
    </section>
  </div>}</DialogContext.Provider>;
}
