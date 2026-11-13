// The command lines of running processes, for the launchers that must not start a second bot.
// Read-only: nothing here stops a process.
import { spawnSync } from 'node:child_process';

/** Every running process as { pid, name, cmd }. An empty list when the listing fails. */
export function runningProcesses() {
  if (process.platform === 'win32') {
    const r = spawnSync(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', 'Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress'],
      { encoding: 'utf8', windowsHide: true, maxBuffer: 64 * 1024 * 1024 },
    );
    if (r.status !== 0 || !r.stdout) return [];
    try {
      const rows = JSON.parse(r.stdout);
      return (Array.isArray(rows) ? rows : [rows]).map((p) => ({ pid: p.ProcessId, name: p.Name ?? '', cmd: p.CommandLine ?? '' }));
    } catch {
      return [];
    }
  }
  const r = spawnSync('ps', ['-eo', 'pid=,comm=,args='], { encoding: 'utf8' });
  if (r.status !== 0 || !r.stdout) return [];
  return r.stdout
    .split('\n')
    .map((l) => /^\s*(\d+)\s+(\S+)\s+(.*)$/.exec(l))
    .filter(Boolean)
    .map((m) => ({ pid: Number(m[1]), name: m[2], cmd: m[3] }));
}

/** The processes that are interpreters or the trader itself, not shells that only mention one. */
export function workers(processes) {
  return processes.filter((p) => /^(python[\d.]*|pythonw|trader|uv|node)(\.exe)?$/i.test(p.name) && p.pid !== process.pid);
}
