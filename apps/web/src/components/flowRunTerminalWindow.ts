export function openStandaloneFlowRunTerminal(runId: string, runName: string) {
  const url = new URL(window.location.href);
  url.search = '';
  url.hash = '';
  url.searchParams.set('terminalRun', runId);
  url.searchParams.set('terminalName', runName);
  window.open(url, `flowweave-terminal-${runId}`, 'popup=yes,width=1280,height=820');
}
