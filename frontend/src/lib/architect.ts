import { getBase } from './api';

export interface ArchitectKpis {
  total: number;
  on: number;
  off: number;
  unavailable: number;
  unreadable: number;
  broken: number;
  stale: number;
  recent: number;
}

export interface ArchitectGroup {
  name: string;
  count: number;
  broken: number;
  stale: number;
  disabled: number;
}

export interface ArchitectOverview {
  ready: boolean;
  refreshing: boolean;
  error: string;
  fetched_at?: number;
  kpis?: ArchitectKpis;
  groups?: ArchitectGroup[];
}

export interface AutomationRow {
  entity_id: string;
  alias: string;
  group: string;
  state: string;
  disabled: boolean;
  broken: boolean;
  stale: boolean;
  age_days: number | null;
  last_triggered: string | null;
  entities: number;
  missing: string[];
  offline: string[];
  readable: boolean;
  via?: string[];
}

export interface AskResult {
  error?: string;
  question?: string;
  matched_on?: string[];
  count?: number;
  active?: number;
  disabled?: number;
  rows?: AutomationRow[];
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${getBase()}/v1/architect${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

export const fetchOverview = () => getJson<ArchitectOverview>('/overview');

export async function startRefresh(): Promise<void> {
  await fetch(`${getBase()}/v1/architect/refresh`, { method: 'POST' });
}

export const fetchAutomations = (group: string, filter: string, q: string) =>
  getJson<{ rows: AutomationRow[] }>(
    `/automations?group=${encodeURIComponent(group)}&filter=${encodeURIComponent(filter)}&q=${encodeURIComponent(q)}`,
  );

export const askArchitect = (q: string) => getJson<AskResult>(`/ask?q=${encodeURIComponent(q)}`);
