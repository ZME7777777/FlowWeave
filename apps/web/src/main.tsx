import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@xyflow/react/dist/style.css';
import './styles.css';
import { App } from './App';
import { AppErrorBoundary } from './components/AppErrorBoundary';

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: 1 } } });

createRoot(document.getElementById('root')!).render(
  <StrictMode><AppErrorBoundary><QueryClientProvider client={queryClient}><App /></QueryClientProvider></AppErrorBoundary></StrictMode>,
);
