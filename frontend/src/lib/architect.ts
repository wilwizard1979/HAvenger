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
  areas?: { id: string; name: string; count: number }[];
  spent_usd?: number;
  limit_usd?: number;
  ha_url?: string;
}

export interface AutomationRow {
  edit_url?: string;
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
  area: string;
  missing: string[];
  offline: string[];
  readable: boolean;
  scenes: number;
  missing_via: string[];
  offline_via: string[];
  via?: string[];
  indirect?: boolean;
}

export interface AskResult {
  error?: string;
  question?: string;
  matched_on?: string[];
  count?: number;
  active?: number;
  disabled?: number;
  indirect?: number;
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

export const fetchAutomations = (group: string, filter: string, q: string, area = '') =>
  getJson<{ rows: AutomationRow[] }>(
    `/automations?group=${encodeURIComponent(group)}&filter=${encodeURIComponent(filter)}&q=${encodeURIComponent(q)}&area=${encodeURIComponent(area)}`,
  );

export const askArchitect = (q: string) => getJson<AskResult>(`/ask?q=${encodeURIComponent(q)}`);

export interface Preview {
  intent: 'lookup' | 'change' | 'create';
  tokens: number;
  token_cap: number;
  over_cap: boolean;
  can_send: boolean;
  dry_run: boolean;
  text: string;
  targets: { entity_id: string; alias: string }[];
  related: { entity_id: string; alias: string }[];
  related_hidden: number;
  disabled_left_out: number;
  picked_disabled: string[];
  estimate_usd: { sonnet: number; opus: number };
  spent_usd: number;
  limit_usd: number;
}

export interface QueryResult {
  error?: string;
  intent?: 'lookup' | 'change' | 'create';
  lookup?: AskResult;
  preview?: Preview;
}

export interface Case {
  id: string;
  question: string;
  intent: string;
  model: string;
  tokens: number;
  cost_usd: number;
  dry_run: boolean;
  answer: string;
  unknown_entities: string[];
  proposal?: { identifier: string; risk: string; edits: unknown[] } | null;
  proposal_problems?: string[];
  execution_id?: number;
  links?: { text: string; url: string }[];
}

export const queryArchitect = (q: string, pick: string[], force = '') =>
  getJson<QueryResult>(`/query?q=${encodeURIComponent(q)}&pick=${encodeURIComponent(pick.join(','))}&force=${force}`);

export async function recommend(q: string, model: 'sonnet' | 'opus', pick: string[], intent = ''): Promise<{ error?: string; case?: Case }> {
  const res = await fetch(`${getBase()}/v1/architect/recommend`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ q, model, pick, intent }),
  });
  return res.json();
}

export const fetchCases = () => getJson<{ cases: Case[] }>('/cases');

export interface Relations {
  id: string;
  title: string;
  lines: string[];
  error?: string;
}

export const fetchRelations = (id: string) => getJson<Relations>(`/relations?id=${encodeURIComponent(id)}`);

export interface ApprovalResult {
  status?: string;
  id?: number;
  problems?: string[];
  error?: string;
}

export async function requestApproval(caseId: string): Promise<ApprovalResult> {
  const res = await fetch(`${getBase()}/v1/architect/cases/${encodeURIComponent(caseId)}/request-approval`, { method: 'POST' });
  return res.json();
}

export async function saveHaUrl(url: string): Promise<{ ha_url?: string; error?: string }> {
  const res = await fetch(`${getBase()}/v1/architect/ha-url`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  return res.json();
}

export async function exportFiles(): Promise<{ dir?: string; files?: Record<string, number>; inventory_age_h?: number; error?: string }> {
  const res = await fetch(`${getBase()}/v1/architect/export`, { method: 'POST' });
  return res.json();
}
