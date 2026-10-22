import { useEffect, useRef, useState } from 'react';

const LOAD_MESSAGE = 'flowweave-browser-preview-load';
const LOAD_TIMEOUT_MS = 25_000;
type PreviewSnapshot = {
  source: string;
  loadId: string;
  html?: string;
  phase: 'fetching' | 'rendering' | 'ready' | 'slow' | 'error';
};

function previewDocument(html: string, source: string, loadId: string): string {
  const document = new DOMParser().parseFromString(html, 'text/html');
  const base = document.querySelector('base');
  if (base) base.href = new URL(base.getAttribute('href') ?? '/', source).href;
  else {
    const fallbackBase = document.createElement('base');
    fallbackBase.href = source;
    document.head.prepend(fallbackBase);
  }
  const probe = document.createElement('script');
  // A sandboxed frame has no readable DOM in the host. The nonce and exact
  // WindowProxy let the host accept only this load's small readiness signal.
  probe.textContent = `(()=>{
    const id=${JSON.stringify(loadId)},kind=${JSON.stringify(LOAD_MESSAGE)};
    let done=false,scheduled=false,observer;
    const report=phase=>{if(!done){done=true;observer?.disconnect();parent.postMessage({kind,id,phase},'*');}};
    const check=()=>{
      if(done||scheduled)return;scheduled=true;
      requestAnimationFrame(()=>requestAnimationFrame(()=>{
        scheduled=false;
        const body=document.body;
        if(!body)return;
        const style=getComputedStyle(body);
        if(style.display==='none'||style.visibility==='hidden'||style.opacity==='0')return;
        const text=body.innerText.trim();
        const media=[...body.querySelectorAll('img,svg,canvas,video,iframe')].some(element=>{
          const rect=element.getBoundingClientRect();
          return rect.width>0&&rect.height>0&&(!(element instanceof HTMLImageElement)||element.naturalWidth>0);
        });
        if(text||media)report('ready');
      }));
    };
    observer=new MutationObserver(check);
    observer.observe(document.documentElement,{childList:true,subtree:true,characterData:true,attributes:true});
    addEventListener('DOMContentLoaded',check);addEventListener('load',check);check();
    addEventListener('error',event=>{
      if(event.target instanceof HTMLScriptElement||event.error)report('error');
    },true);
    addEventListener('unhandledrejection',()=>report('error'));
    addEventListener('pagehide',()=>observer.disconnect(),{once:true});
  })();`;
  document.head.prepend(probe);
  return `<!doctype html>${document.documentElement.outerHTML}`;
}

export function RuntimeBrowserFrame({ source, title }: { source: string; title: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [attempt, setAttempt] = useState(0);
  const [snapshot, setSnapshot] = useState<PreviewSnapshot>();
  useEffect(() => {
    const controller = new AbortController();
    const loadId = crypto.randomUUID();
    let timer: ReturnType<typeof setTimeout>;
    const updatePhase = (phase: PreviewSnapshot['phase']) => {
      if (controller.signal.aborted) return;
      setSnapshot(current => current?.loadId === loadId ? { ...current, phase } : current);
    };
    const startTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(() => updatePhase('slow'), LOAD_TIMEOUT_MS);
    };
    const receive = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== 'null') return;
      const message = event.data as { kind?: unknown; id?: unknown; phase?: unknown } | null;
      if (!message || message.kind !== LOAD_MESSAGE || message.id !== loadId) return;
      if (message.phase !== 'ready' && message.phase !== 'error') return;
      clearTimeout(timer);
      updatePhase(message.phase);
    };
    window.addEventListener('message', receive);
    setSnapshot({ source, loadId, phase: 'fetching' });
    startTimeout();
    // Authenticate only the initial HTML in the host. Resources use the scoped
    // preview grant, and the iframe retains its opaque sandbox origin.
    void fetch(source, { credentials: 'include', signal: controller.signal })
      .then(async response => {
        const html = await response.text();
        if (!response.ok && !response.headers.get('content-type')?.includes('text/html')) throw new Error('preview');
        if (controller.signal.aborted) return;
        setSnapshot({ source, loadId, html: previewDocument(html, source, loadId), phase: 'rendering' });
        startTimeout();
      })
      .catch(() => {
        clearTimeout(timer);
        updatePhase('error');
      });
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener('message', receive);
    };
  }, [source, attempt]);
  const current = snapshot?.source === source ? snapshot : undefined;
  const phase = current?.phase ?? 'fetching';
  const waiting = phase === 'fetching' || phase === 'rendering';
  return <div className="agent-workspace-browser-content" aria-busy={waiting || phase === 'slow'}>
    {current?.html && <iframe ref={frame} key={current.loadId} className="agent-workspace-browser-frame" title={title} srcDoc={current.html} sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads" referrerPolicy="no-referrer"/>}
    {phase !== 'ready' && <div className="agent-workspace-browser-loading" role={waiting ? 'status' : 'alert'}>
      {waiting && <i className="agent-workspace-browser-loading-spinner" aria-hidden="true"/>}
      <b>{waiting ? '正在加载页面…' : phase === 'slow' ? '页面加载较慢' : '页面加载失败'}</b>
      <span>{waiting ? phase === 'fetching' ? '正在连接服务' : '正在加载页面资源' : phase === 'slow' ? '仍在等待页面显示，你也可以重新加载。' : '请检查服务状态后重试。'}</span>
      {!waiting && <button type="button" onClick={() => setAttempt(value => value + 1)}>重新加载页面</button>}
    </div>}
  </div>;
}
