import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@xyflow/react/dist/style.css';
import './styles.css';
import { App } from './App';
import { AppErrorBoundary } from './components/AppErrorBoundary';
import { ProductDialogProvider } from './components/ProductDialog';

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: 1 } } });

const appEntry = document.querySelector<HTMLScriptElement>('script[type="module"][src]')?.src;
let updateReloading = false;

async function reloadWhenDeploymentChanges(): Promise<void> {
  if (!appEntry || updateReloading || document.visibilityState !== 'visible') return;
  try {
    const response = await fetch(`/?deployment-check=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) return;
    const html = await response.text();
    const match = html.match(/<script[^>]+type=["']module["'][^>]+src=["']([^"']+)["']/i)
      ?? html.match(/<script[^>]+src=["']([^"']+)["'][^>]+type=["']module["']/i);
    if (!match) return;
    const deployedEntry = new URL(match[1], window.location.href).href;
    if (deployedEntry !== appEntry) {
      updateReloading = true;
      window.location.reload();
    }
  } catch {
    // A transient network failure must not disturb the active conversation.
  }
}

window.setInterval(() => { void reloadWhenDeploymentChanges(); }, 30_000);
window.addEventListener('focus', () => { void reloadWhenDeploymentChanges(); });
document.addEventListener('visibilitychange', () => { void reloadWhenDeploymentChanges(); });

createRoot(document.getElementById('root')!).render(
  <StrictMode><AppErrorBoundary><QueryClientProvider client={queryClient}><ProductDialogProvider><App /></ProductDialogProvider></QueryClientProvider></AppErrorBoundary></StrictMode>,
);
