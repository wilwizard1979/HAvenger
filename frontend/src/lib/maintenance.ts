import { useCallback, useEffect, useRef, useState } from 'react';
import { getBase } from './api';

export type Tone = 'ok' | 'warn' | 'bad' | 'idle';
export type Severity = 'critical' | 'warning' | 'info';
export type Effort = 'quick' | 'medium' | 'hard';

export interface SectionItem {
  fingerprint?: string;
  title: string;
  severity: Severity;
  state?: string;
  category?: string;
  first_seen?: number;
  detail?: string;
}

export interface Overview {
  cards: {
    stale_unavailable: { value: number; days: number; tone: Tone };
    last_restart: { value: number | null; unit: string; tone: Tone };
    backup_failed: { value: number; tone: Tone };
    last_backup: { value: number | null; unit: string; at: string | null; limit_hours: number; tone: Tone };
    integrations_failed: { value: number; tone: Tone };
    automation_errors: { value: number; tone: Tone };
    updates_pending: { value: number | null; tone: Tone };
    deprecations: { value: number; next_version: string | null; tone: Tone };
    log_critical: { value: number | null; tone: Tone };
  };
  sections: Record<
    string,
    { total: number; items: SectionItem[]; counting_since?: number | null; reliable_from?: number | null; tracked?: number }
  >;
  checked_at: number | null;
  ha_version: string | null;
}

export interface InstructionStep {
  tool: string;
  args: Record<string, unknown>;
  purpose: string;
  verify: string;
}

export interface Proposal {
  summary: string;
  mode?: 'quick' | 'investigate' | 'deep';
  tool_calls?: number;
  diagnosis?: string;
  instruction?: { preconditions: string[]; steps: InstructionStep[]; rollback_steps: InstructionStep[] };
  steps?: string[];
  risk: 'low' | 'medium' | 'high';
  breaking_change_check: string;
  rollback?: string;
  verification: string[];
  needs_user: boolean;
  user_action: string;
}

export type EscalationMode = 'quick' | 'investigate' | 'deep';

export interface Estimate {
  tokens_in: number;
  tokens_out: number;
  usd: number | null;
  rounds?: number;
}

export interface Task {
  id: string;
  label: string;
  severity: Severity;
  effort: Effort;
  effort_source: 'override' | 'rule' | 'haiku' | 'default';
  kind: 'change' | 'chore';
  needs_user: boolean;
  quadrant: 'quick_win' | 'scheduled' | 'major_project' | 'defer' | 'fill_in';
  count: number;
  deadline: string | null;
  note: string | null;
  cause: string | null;
  examples: string[];
  selected: boolean;
  proposal: Proposal | null;
  proposal_stale: boolean;
  estimate: Estimate;
  estimate_investigate: Estimate;
  estimate_deep: Estimate;
}

interface Sum {
  tasks: number;
  tokens_in: number;
  tokens_out: number;
  usd: number | null;
}

export interface Plan {
  tasks: Task[];
  lanes: Record<string, string[]>;
  matrix: {
    cells: Record<Severity, Record<Effort, { clusters: number; issues: number }>>;
    quick_wins: string[];
    chores: string[];
    needs_user: string[];
  };
  estimates: {
    model: string;
    proposals_all_pending: Sum;
    proposals_selected_pending: Sum;
    triage_next_run: { model: string; clusters: number; usd: number };
    local_readout_tokens_per_digest: number;
    budget: { limit_usd: number; spent_usd: number; remaining_usd: number };
    assumptions: string[];
  };
  dry_run: boolean;
}

export interface ReportOut {
  id?: number;
  status?: string;
  text?: string;
  triage?: { status: string; [k: string]: unknown };
}

export interface LatestReport {
  report: { id: number; created_at: number; status: string; summary_text: string } | null;
}

export interface RunRow {
  id: number;
  model: string | null;
  started: number;
  outcome: string | null;
  purpose: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  dry_run: number;
}

export interface Team {
  dry_run: boolean;
  l0: {
    role: string;
    interval_min: number | null;
    next_run: number | null;
    last_tick: {
      ok: boolean;
      started: number;
      duration_s: number;
      observed: number;
      new: unknown[];
      resolved: unknown[];
      errors: Record<string, string>;
    } | null;
    issues: { active: number; by_severity: Record<string, number> } | null;
    report: { id: number; created_at: number; status: string; text: string } | null;
  };
  l1: {
    role: string;
    latest: {
      created_at: number;
      model: string;
      tokens_in: number;
      tokens_out: number;
      seconds: number;
      text: string;
      guard_ok: number;
      note: string;
    } | null;
    runs: RunRow[];
  };
  l2: {
    role: string;
    model: string;
    mode: string;
    runs: RunRow[];
    reasoning: { cluster_id: string; label: string; note: string; cause: string; effort: string | null; updated: number }[];
  };
  l3: {
    role: string;
    model: string;
    auto_escalation: boolean;
    escalated: { cluster_id: string; label: string; risk: string; summary: string; cost_usd: number; updated: number }[];
    runs: RunRow[];
  };
  l4: { role: string; model: string; runs: RunRow[] };
  month: { model: string; calls: number; tokens_in: number; tokens_out: number; cost_usd: number }[];
}

const BASE = () => `${getBase()}/v1/ha/agents`;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE()}${path}`, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (typeof j.detail === 'string') detail = j.detail;
    } catch {
      /* keep the status text */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  overview: () => call<Overview>('/overview'),
  plan: () => call<Plan>('/plan'),
  select: (id: string, selected: boolean) =>
    call<Plan>('/plan/select', { method: 'PUT', body: JSON.stringify({ id, selected }) }),
  override: (id: string, effort: Effort | null) =>
    call('/clusters/override', { method: 'PUT', body: JSON.stringify({ id, effort }) }),
  proposal: (id: string, mode: EscalationMode) =>
    call<{ status: string; estimate?: Estimate; cost_usd?: number; detail?: string; tool_calls?: number }>('/plan/proposal', {
      method: 'POST',
      body: JSON.stringify({ id, mode }),
    }),
  latestReport: () => call<LatestReport>('/reports/latest'),
  runReport: () => call<ReportOut>('/reports/run?triage=true', { method: 'POST' }),
  team: () => call<Team>('/team'),
  runReadout: () => call<{ status: string; detail?: string }>('/readout/run', { method: 'POST' }),
  runTriage: () => call<{ status: string; estimated_cost_usd?: number; detail?: string }>('/triage/run', { method: 'POST' }),
  runWatchdog: () => call<unknown>('/watchdog/run', { method: 'POST' }),
};

/** Polls a Janitor endpoint; `reload` fetches again right away. */
export function usePolled<T>(fetcher: () => Promise<T>, everyMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetcherRef.current();
      if (alive.current) {
        setData(next);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void reload();
    const id = setInterval(() => void reload(), everyMs);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [reload, everyMs]);

  return { data, error, loading, reload, setData };
}

export const TONE_COLOR: Record<Tone, string> = {
  ok: 'var(--color-success)',
  warn: 'var(--color-warning)',
  bad: 'var(--color-error)',
  idle: 'var(--color-text-tertiary)',
};

export const SEVERITY_COLOR: Record<Severity, string> = {
  critical: 'var(--color-error)',
  warning: 'var(--color-warning)',
  info: 'var(--color-text-tertiary)',
};

export const usd = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : v < 0.01 && v > 0 ? '<$0.01' : `$${v.toFixed(2)}`;

export const tokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
