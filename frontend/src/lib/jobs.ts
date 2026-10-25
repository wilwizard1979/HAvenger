import { useEffect, useState, useSyncExternalStore } from 'react';
import { api, usd, type EscalationMode } from './maintenance';

/**
 * Escalations (Sonnet / Opus) take 30 to 90 seconds. They live here, outside any
 * page, so the progress stays visible when you switch tabs and a second click on
 * the same task is ignored.
 */
export interface Job {
  key: string;
  taskId: string;
  label: string;
  mode: EscalationMode;
  startedAt: number;
  state: 'running' | 'done' | 'failed';
  message: string;
  seen: boolean;
}

let jobs: Job[] = [];
const listeners = new Set<() => void>();
const finished = new Set<(j: Job) => void>();

const emit = () => listeners.forEach((l) => l());
const set = (next: Job[]) => {
  jobs = next;
  emit();
};

export const MODE_NAME: Record<EscalationMode, string> = {
  quick: 'Proposing',
  investigate: 'Investigating',
  deep: 'Deep analysis (Opus) of',
};

export const useJobs = () => useSyncExternalStore((cb) => (listeners.add(cb), () => listeners.delete(cb)), () => jobs);

export const onJobFinished = (cb: (j: Job) => void) => {
  finished.add(cb);
  return () => {
    finished.delete(cb);
  };
};

export const dismissJob = (key: string) => set(jobs.filter((j) => j.key !== key));
export const markSeen = (key: string) => set(jobs.map((j) => (j.key === key ? { ...j, seen: true } : j)));
export const isRunning = (taskId: string) => jobs.some((j) => j.taskId === taskId && j.state === 'running');

/** Elapsed seconds since `since`, refreshed every second while mounted. */
export function useElapsed(since: number | null) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (since === null) return;
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [since]);
  return since === null ? 0 : Math.max(0, Math.round((Date.now() - since) / 1000));
}

function describe(r: Awaited<ReturnType<typeof api.proposal>>): { ok: boolean; text: string } {
  switch (r.status) {
    case 'ok': {
      const p = r.proposal;
      const steps = p?.instruction?.steps.length ?? 0;
      const bits = [
        p ? `risk ${p.risk}` : '',
        steps ? `${steps} executable step${steps === 1 ? '' : 's'}` : 'no executable steps (manual work or nothing to change)',
        r.tool_calls ? `${r.tool_calls} lookups` : '',
        r.cost_usd !== undefined ? usd(r.cost_usd) : '',
      ].filter(Boolean);
      return { ok: true, text: `Proposal written: ${bits.join(' · ')}.` };
    }
    case 'dry_run':
      return { ok: false, text: 'Dry-run is ON, so nothing was sent and no proposal was written. Switch it off in Settings.' };
    case 'already_running':
      return { ok: false, text: 'This task is already being worked on.' };
    case 'skipped_budget':
      return { ok: false, text: `The monthly budget guard stopped it. ${r.detail ?? ''}` };
    case 'skipped_no_key':
      return { ok: false, text: 'No Anthropic key found in the Keychain.' };
    case 'bad_reply':
      return { ok: false, text: 'The model answered, but not in a form I could read. Try again.' };
    default:
      return { ok: false, text: `${r.status}${r.detail ? `: ${r.detail}` : ''}` };
  }
}

export async function startEscalation(
  task: { id: string; label: string },
  mode: EscalationMode,
  focus?: string,
): Promise<Job | null> {
  if (isRunning(task.id)) return null;
  const job: Job = {
    key: `${task.id}:${Date.now()}`,
    taskId: task.id,
    label: task.label,
    mode,
    startedAt: Date.now(),
    state: 'running',
    message: '',
    seen: false,
  };
  set([job, ...jobs.filter((j) => j.taskId !== task.id)]);
  let result: { ok: boolean; text: string };
  try {
    result = describe(await api.proposal(task.id, mode, focus));
  } catch (e) {
    result = { ok: false, text: e instanceof Error ? e.message : 'The request failed' };
  }
  const done: Job = { ...job, state: result.ok ? 'done' : 'failed', message: result.text };
  set(jobs.map((j) => (j.key === job.key ? done : j)));
  finished.forEach((cb) => cb(done));
  return done;
}
