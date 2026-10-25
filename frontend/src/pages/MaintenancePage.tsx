import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { SettingsTab } from './MaintenanceSettings';
import { ChevronDown, ChevronRight, RefreshCw, Play, Volume2, Square } from 'lucide-react';
import { useReadAloud } from '../lib/janitorSpeech';
import {
  api,
  usePolled,
  usd,
  tokens,
  TONE_COLOR,
  SEVERITY_COLOR,
  type Effort,
  type EscalationMode,
  type Execution,
  type Overview,
  type Proposal,
  type Plan,
  type Severity,
  type RunRow,
  type Task,
  type Team,
} from '../lib/maintenance';

type Tab = 'dashboard' | 'plan' | 'flow' | 'reports' | 'settings';

const TABS: { id: Tab; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'flow', label: 'Flow' },
  { id: 'plan', label: 'Plan' },
  { id: 'reports', label: 'Reports' },
  { id: 'settings', label: 'Settings' },
];

const muted = { color: 'var(--color-text-secondary)' } as const;
const faint = { color: 'var(--color-text-tertiary)' } as const;

function Chip({ text, color }: { text: string; color?: string }) {
  return (
    <span
      className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide shrink-0"
      style={{
        color: color ?? 'var(--color-text-secondary)',
        border: `1px solid ${color ?? 'var(--color-border)'}`,
      }}
    >
      {text}
    </span>
  );
}

function ago(ts?: number | null) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

// --- Dashboard -----------------------------------------------------------------

const SECTIONS: { key: string; title: string; hint: string }[] = [
  { key: 'backup_failed', title: 'Backup failed', hint: 'Failing backup targets and an outdated last backup' },
  { key: 'last_backup', title: 'Last successful backup', hint: 'Time of the newest good backup' },
  { key: 'integrations_failed', title: 'Integrations failed', hint: 'Config entries that did not load' },
  { key: 'automation_errors', title: 'Automation errors', hint: 'Automations Home Assistant flags as broken' },
  { key: 'updates_pending', title: 'Updates pending', hint: 'Install after a backup and a breaking-change check' },
  { key: 'deprecations', title: 'Deprecations and breaking changes', hint: 'Will stop working in a coming release' },
  { key: 'log_critical', title: 'Critical errors in the log', hint: 'Last 400 log lines' },
  { key: 'motion_batteries', title: 'Motion sensor batteries', hint: 'Every motion sensor, lowest battery first' },
  { key: 'log_levels', title: 'Log: warnings, errors and critical', hint: 'Only these levels, grouped by component. Newest 400 lines' },
  { key: 'stale_unavailable', title: 'Unavailable for a long time', hint: 'Oldest first. Likely orphaned or dead devices' },
  { key: 'last_restart', title: 'Home Assistant restarts', hint: 'When Home Assistant last started, as seen by the watchdog' },
];

function cardOf(o: Overview, key: string) {
  return (o.cards as unknown as Record<string, { value: number | null; tone: keyof typeof TONE_COLOR }>)[key];
}

function DashboardTab({ o, initial }: { o: Overview; initial: string | null }) {
  const [open, setOpen] = useState<Set<string>>(new Set(initial ? [initial] : []));
  const toggle = (k: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  return (
    <div className="flex flex-col gap-2">
      {SECTIONS.map(({ key, title, hint }) => {
        const card = cardOf(o, key);
        const sec = o.sections[key];
        const isOpen = open.has(key);
        const lb = o.cards.last_backup;
        const value = key === 'last_restart' ? (o.cards.last_restart.value === null ? '—' : `${Math.round(o.cards.last_restart.value)} h ago`) : key === 'last_backup' ? (lb.value === null ? '—' : `${Math.round(lb.value)} h ago`) : (card.value ?? '—');
        return (
          <div key={key} id={key} className="hud-panel" style={{ borderLeft: `2px solid ${TONE_COLOR[card.tone]}` }}>
            <button
              onClick={() => toggle(key)}
              className="w-full flex items-center gap-3 px-3 py-2.5 cursor-pointer text-left"
              aria-expanded={isOpen}
            >
              {isOpen ? <ChevronDown size={14} style={muted} /> : <ChevronRight size={14} style={muted} />}
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
                  {title}
                </div>
                <div className="text-[11px]" style={faint}>
                  {hint}
                </div>
              </div>
              <span className="text-lg font-semibold hud-mono" style={{ color: TONE_COLOR[card.tone] }}>
                {value}
              </span>
            </button>
            {isOpen && (
              <div className="px-3 pb-3 pt-1" style={{ borderTop: '1px solid var(--color-border)' }}>
                {key === 'last_backup' ? (
                  <div className="text-xs" style={muted}>
                    {lb.at ? `Last successful backup: ${new Date(lb.at).toLocaleString()}` : 'No successful backup time known.'}{' '}
                    Limit: {lb.limit_hours} h.
                  </div>
                ) : !sec || sec.items.length === 0 ? (
                  <div className="text-xs" style={faint}>
                    Nothing to show.
                  </div>
                ) : (
                  <ul className="flex flex-col gap-1.5 mt-1">
                    {sec.items.map((it, i) => (
                      <li key={it.fingerprint ?? i} className="text-xs flex flex-col gap-0.5">
                        <div className="flex items-center gap-2">
                          <Chip text={it.severity} color={SEVERITY_COLOR[it.severity]} />
                          <span className="min-w-0 truncate" style={{ color: 'var(--color-text)' }} title={it.title}>
                            {it.title}
                          </span>
                          {it.first_seen && (
                            <span className="ml-auto shrink-0" style={faint}>
                              {ago(it.first_seen)}
                            </span>
                          )}
                        </div>
                        {it.detail && (
                          <div className="hud-mono text-[10px] truncate pl-1" style={faint} title={it.detail}>
                            {it.detail}
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {key === 'stale_unavailable' && sec && (
                  <div className="text-[11px] mt-2" style={faint}>
                    Home Assistant resets its change time at every restart, so the Janitor counts from {sec.counting_since ? new Date(sec.counting_since * 1000).toLocaleDateString() : 'its first check'} on
                    {sec.reliable_from ? `; the result is complete from ${new Date(sec.reliable_from * 1000).toLocaleDateString()}` : ''}.
                    {sec.tracked !== undefined ? ` Entities tracked: ${sec.tracked}.` : ''}
                  </div>
                )}
                {key === 'last_restart' && (
                  <div className="text-[11px] mt-2" style={faint}>
                    Home Assistant does not say whether a start was a crash or planned; a start right after an unreachable check is flagged.
                  </div>
                )}
                {sec && key !== 'stale_unavailable' && sec.total > sec.items.length && (
                  <div className="text-[11px] mt-2" style={faint}>
                    Showing the top {sec.items.length} of {sec.total}.
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// --- Plan ------------------------------------------------------------------------

const SEVERITIES: Severity[] = ['critical', 'warning', 'info'];
const EFFORTS: Effort[] = ['quick', 'medium', 'hard'];
const LANE_TITLES: Record<string, string> = {
  quick_win: 'Quick wins',
  scheduled: 'Scheduled',
  major_project: 'Major projects',
  defer: 'Deferred',
  fill_in: 'Fill-ins',
};

function Matrix({ plan }: { plan: Plan }) {
  return (
    <div className="hud-panel p-3">
      <div className="hud-label mb-2">Severity × effort (clusters / issues)</div>
      <div className="grid gap-1" style={{ gridTemplateColumns: '90px repeat(3, 1fr)' }}>
        <div />
        {EFFORTS.map((e) => (
          <div key={e} className="text-[11px] text-center uppercase tracking-wide" style={faint}>
            {e}
          </div>
        ))}
        {SEVERITIES.map((s) => (
          <div key={s} className="contents">
            <div className="text-[11px] uppercase tracking-wide self-center" style={{ color: SEVERITY_COLOR[s] }}>
              {s}
            </div>
            {EFFORTS.map((e) => {
              const cell = plan.matrix.cells[s][e];
              const hot = cell.clusters > 0;
              return (
                <div
                  key={e}
                  className="rounded-md text-center py-2 hud-mono text-sm"
                  style={{
                    background: hot ? `color-mix(in srgb, ${SEVERITY_COLOR[s]} 14%, transparent)` : 'var(--color-bg-secondary)',
                    color: hot ? 'var(--color-text)' : 'var(--color-text-tertiary)',
                    border: '1px solid var(--color-border)',
                  }}
                >
                  {cell.clusters} <span style={faint}>/ {cell.issues}</span>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function Estimates({ plan }: { plan: Plan }) {
  const [more, setMore] = useState(false);
  const e = plan.estimates;
  const row = (label: string, s: { tasks: number; tokens_in: number; tokens_out: number; usd: number | null }) => (
    <div className="flex items-center justify-between text-xs py-1">
      <span style={muted}>
        {label} <span style={faint}>({s.tasks} tasks)</span>
      </span>
      <span className="hud-mono" style={{ color: 'var(--color-text)' }}>
        {tokens(s.tokens_in)} in + {tokens(s.tokens_out)} out · {usd(s.usd)}
      </span>
    </div>
  );
  return (
    <div className="hud-panel p-3">
      <div className="hud-label mb-1">Tokens and cost (paid, {e.model})</div>
      {row('Proposals for the tasks you ticked', e.proposals_selected_pending)}
      {row('Proposals for all open tasks', e.proposals_all_pending)}
      <div className="flex items-center justify-between text-xs py-1">
        <span style={muted}>
          Next triage run <span style={faint}>({e.triage_next_run.model}, {e.triage_next_run.clusters} clusters)</span>
        </span>
        <span className="hud-mono" style={{ color: 'var(--color-text)' }}>
          {usd(e.triage_next_run.usd)}
        </span>
      </div>
      <div className="flex items-center justify-between text-xs py-1">
        <span style={muted}>Local voice read-out (Qwen)</span>
        <span className="hud-mono" style={{ color: 'var(--color-text)' }}>
          ~{e.local_readout_tokens_per_digest} tokens · free
        </span>
      </div>
      <div className="flex items-center justify-between text-xs py-1" style={{ borderTop: '1px solid var(--color-border)' }}>
        <span style={muted}>Budget this month</span>
        <span className="hud-mono" style={{ color: 'var(--color-text)' }}>
          {usd(e.budget.spent_usd)} of {usd(e.budget.limit_usd)} · {usd(e.budget.remaining_usd)} left
        </span>
      </div>
      <div className="text-[11px] mt-1" style={faint}>
        {plan.dry_run
          ? 'Dry-run is ON: requesting a proposal makes no cloud call and costs nothing.'
          : 'Dry-run is OFF: requesting a proposal is a paid cloud call.'}{' '}
        <button className="underline cursor-pointer" onClick={() => setMore(!more)}>
          {more ? 'hide assumptions' : 'assumptions'}
        </button>
      </div>
      {more && (
        <ul className="text-[11px] mt-1 list-disc pl-4 space-y-0.5" style={faint}>
          {e.assumptions.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ProposalView({ p }: { p: Proposal }) {
  return (
        <div className="text-xs flex flex-col gap-1.5 pt-2" style={{ borderTop: '1px solid var(--color-border)', color: 'var(--color-text)' }}>
          <div>{p.summary}</div>
          <div className="flex items-center gap-2">
            <Chip text={`risk ${p.risk}`} color={p.risk === 'high' ? 'var(--color-error)' : p.risk === 'medium' ? 'var(--color-warning)' : 'var(--color-success)'} />
            {p.needs_user && <Chip text="needs you" />}
          </div>
          {p.diagnosis && (
            <div style={muted}>
              <b>Diagnosis:</b> {p.diagnosis}
            </div>
          )}
          {p.steps && p.steps.length > 0 && (
            <ol className="list-decimal pl-5 space-y-0.5">
              {p.steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          )}
          {p.instruction && (
            <div className="flex flex-col gap-1">
              {p.instruction.preconditions.length > 0 && (
                <div style={muted}>
                  <b>Before:</b> {p.instruction.preconditions.join(' · ')}
                </div>
              )}
              <div className="hud-label">Instruction for the executor ({p.instruction.steps.length} steps, not run)</div>
              {p.instruction.steps.length === 0 && <div style={faint}>No change in Home Assistant is needed.</div>}
              <ol className="list-decimal pl-5 space-y-1">
                {p.instruction.steps.map((st, i) => (
                  <li key={i}>
                    <span className="hud-mono">{st.tool}</span> <span style={faint}>{st.purpose}</span>
                    <div className="hud-mono text-[10px] break-all" style={faint}>
                      {JSON.stringify(st.args)}
                    </div>
                    {st.verify && <div style={faint}>check: {st.verify}</div>}
                  </li>
                ))}
              </ol>
              {p.instruction.rollback_steps.length > 0 && (
                <div style={muted}>
                  <b>Rollback:</b> {p.instruction.rollback_steps.map((r) => `${r.tool} ${JSON.stringify(r.args)}`).join(' → ')}
                </div>
              )}
            </div>
          )}
          <div style={muted}>
            <b>Breaking-change check:</b> {p.breaking_change_check}
          </div>
          {p.rollback && (
            <div style={muted}>
              <b>Rollback:</b> {p.rollback}
            </div>
          )}
          {p.verification.length > 0 && (
            <div style={muted}>
              <b>Verify:</b> {p.verification.join(' · ')}
            </div>
          )}
          {p.user_action && (
            <div style={muted}>
              <b>Your action:</b> {p.user_action}
            </div>
          )}
          <div className="text-[11px]" style={faint}>
            Proposal only. Nothing is applied before the approval step exists and you confirm it.
          </div>
        </div>
  );
}

function TaskRow({ t, onChange }: { t: Task; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      onChange();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed');
    } finally {
      setBusy(false);
    }
  };

  // Plan tab ladder: Propose first; once a proposal exists the next step up
  // (Investigate, then Deep). The Flow tab keeps every mode for debugging.
  const have: EscalationMode | null = t.proposal ? (t.proposal.mode ?? 'quick') : null;
  const nextMode: EscalationMode | null =
    t.proposal_stale || have === null ? 'quick' : have === 'quick' ? 'investigate' : have === 'investigate' ? 'deep' : null;

  const escalate = (mode: EscalationMode) =>
    run(async () => {
      const est = mode === 'quick' ? t.estimate : mode === 'investigate' ? t.estimate_investigate : t.estimate_deep;
      const r = await api.proposal(t.id, mode);
      if (r.status === 'dry_run')
        setMsg(`Dry-run: would use up to ${tokens(est.tokens_in + est.tokens_out)} tokens (${usd(est.usd)}). Nothing was sent.`);
      else if (r.status !== 'ok') setMsg(`${r.status}${r.detail ? `: ${r.detail}` : ''}`);
      else setOpen(true);
    });

  return (
    <div className="hud-panel p-3 flex flex-col gap-2" style={{ borderLeft: `2px solid ${SEVERITY_COLOR[t.severity]}` }}>
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          className="mt-1 cursor-pointer"
          checked={t.selected}
          disabled={busy}
          onChange={(e) => void run(() => api.select(t.id, e.target.checked))}
          aria-label={`Select ${t.label}`}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
              {t.label}
            </span>
            <Chip text={t.severity} color={SEVERITY_COLOR[t.severity]} />
            <Chip text={t.effort} />
            <Chip text={t.kind === 'chore' ? 'chore · no change in HA' : 'needs your approval'} />
            {t.deadline && <Chip text={`breaks in ${t.deadline}`} color="var(--color-warning)" />}
            <span className="text-[11px]" style={faint}>
              {t.count} issue{t.count === 1 ? '' : 's'}
            </span>
          </div>
          {(t.note || t.cause) && (
            <div className="text-xs mt-1" style={muted}>
              {t.note}
              {t.cause ? ` · likely cause: ${t.cause}` : ''}
            </div>
          )}
          <div className="text-[11px] mt-1 truncate" style={faint} title={t.examples.join(' | ')}>
            {t.examples.join(' · ')}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <span className="text-[11px] hud-mono" style={faint}>
            propose ≈ {tokens(t.estimate.tokens_in + t.estimate.tokens_out)} tok · {usd(t.estimate.usd)}
          </span>
          <div className="flex items-center gap-1.5">
            <select
              value={t.effort_source === 'override' ? t.effort : ''}
              onChange={(e) => void run(() => api.override(t.id, (e.target.value || null) as Effort | null))}
              className="text-[11px] rounded px-1 py-0.5 cursor-pointer"
              style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)' }}
              aria-label="Effort override"
              title="Override the effort rating"
            >
              <option value="">effort: {t.effort_source}</option>
              {EFFORTS.map((e) => (
                <option key={e} value={e}>
                  set {e}
                </option>
              ))}
            </select>
            {t.proposal ? (
              <button
                onClick={() => setOpen(!open)}
                className="text-[11px] px-2 py-1 rounded cursor-pointer"
                style={{ background: 'var(--color-accent-subtle)', border: '1px solid var(--color-accent)', color: 'var(--color-text)' }}
              >
                {open ? 'Hide proposal' : 'Show proposal'}
              </button>
            ) : null}
          </div>
          <div className="flex items-center gap-1.5 flex-wrap justify-end">
            {(
              [
                ['quick', 'Propose', t.estimate, 'Sonnet writes a plan from what the Janitor already knows'],
                ['investigate', 'Investigate', t.estimate_investigate, 'Sonnet also looks at Home Assistant (read-only) and writes the steps to run'],
                ['deep', 'Deep (Opus)', t.estimate_deep, 'Opus, read-only lookups, for hard cases'],
              ] as [EscalationMode, string, { usd: number | null }, string][]
            )
              .filter(([mode]) => mode === nextMode)
              .map(([mode, label, est, tip]) => (
                <button
                  key={mode}
                  disabled={busy}
                  onClick={() => void escalate(mode)}
                  className="text-[11px] px-2 py-1 rounded cursor-pointer flex items-center gap-1"
                  style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
                  title={`${tip}. Started by you only. Up to ${usd(est.usd)}.`}
                >
                  <Play size={10} /> {t.proposal_stale ? 'Refresh' : label} ≤{usd(est.usd)}
                </button>
              ))}
          </div>
        </div>
      </div>
      {msg && (
        <div className="text-[11px]" style={muted}>
          {msg}
        </div>
      )}
      {open && t.proposal && <ProposalView p={t.proposal} />}
    </div>
  );
}

function PlanTab({ plan, reload }: { plan: Plan; reload: () => void }) {
  const byId = useMemo(() => new Map(plan.tasks.map((t) => [t.id, t])), [plan]);
  const lanes = Object.keys(LANE_TITLES).filter((l) => (plan.lanes[l] ?? []).length > 0);
  const ticked = plan.tasks.filter((t) => t.selected).length;
  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.2fr)' }}>
        <Matrix plan={plan} />
        <Estimates plan={plan} />
      </div>
      <div className="text-xs" style={muted}>
        {plan.tasks.length} tasks, quick wins first (a deadline moves a task to the top). You decide which to fix: tick a task, request its
        proposal when you want one. {ticked} ticked.
      </div>
      {plan.tasks.length === 0 && (
        <div className="text-sm" style={faint}>
          No open tasks yet. The first watchdog checks run a few minutes after the server starts.
        </div>
      )}
      {lanes.map((lane) => (
        <section key={lane} className="flex flex-col gap-2">
          <div className="hud-label">
            {LANE_TITLES[lane]} · {plan.lanes[lane].length}
          </div>
          {plan.lanes[lane].map((id) => {
            const t = byId.get(id);
            return t ? <TaskRow key={id} t={t} onChange={reload} /> : null;
          })}
        </section>
      ))}
    </div>
  );
}

// --- Agents ----------------------------------------------------------------------

function Runs({ runs }: { runs: RunRow[] }) {
  if (runs.length === 0)
    return (
      <div className="text-xs" style={faint}>
        No runs yet.
      </div>
    );
  return (
    <ul className="flex flex-col gap-0.5">
      {runs.map((r) => (
        <li key={r.id} className="text-[11px] hud-mono flex gap-2" style={muted}>
          <span style={faint}>{new Date(r.started * 1000).toLocaleString()}</span>
          <span>{r.outcome}</span>
          {r.purpose && <span className="truncate" style={faint}>{r.purpose}</span>}
          <span className="ml-auto shrink-0">
            {tokens(r.input_tokens)} in · {tokens(r.output_tokens)} out · {usd(r.cost_usd)}
          </span>
        </li>
      ))}
    </ul>
  );
}

type StepKey = 'check' | 'digest' | 'qwen' | 'haiku' | 'sonnet' | 'exec';

function Step({
  n,
  tag,
  title,
  role,
  status,
  open,
  onToggle,
  action,
  children,
}: {
  n: number;
  tag: string;
  title: string;
  role: string;
  status: string;
  open: boolean;
  onToggle: () => void;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="hud-panel">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <button onClick={onToggle} className="flex items-center gap-3 flex-1 min-w-0 text-left cursor-pointer" aria-expanded={open}>
          {open ? <ChevronDown size={14} style={muted} /> : <ChevronRight size={14} style={muted} />}
          <span className="hud-mono text-xs" style={faint}>
            {n}
          </span>
          <Chip text={tag} color="var(--color-accent)" />
          <div className="min-w-0">
            <div className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
              {title}
            </div>
            <div className="text-[11px] truncate" style={faint}>
              {role}
            </div>
          </div>
        </button>
        <span className="text-[11px] hud-mono shrink-0" style={muted}>
          {status}
        </span>
        {action}
      </div>
      {open && (
        <div className="px-3 pb-3 pt-2 flex flex-col gap-2" style={{ borderTop: '1px solid var(--color-border)' }}>
          {children}
        </div>
      )}
    </section>
  );
}

function FlowTab({ plan }: { plan: Plan | null }) {
  const navigate = useNavigate();
  const speech = useReadAloud();
  const house = usePolled(api.houseStatus, 60_000);
  const { data, reload } = usePolled(api.team, 20_000);
  const settings = usePolled(api.settings, 60_000);
  const execs = usePolled(api.executions, 10_000);
  const tg = usePolled(api.telegramStatus, 20_000);
  const [busy, setBusy] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<Set<StepKey>>(new Set());
  const [mode, setMode] = useState<EscalationMode>('quick');
  const [confirm, setConfirm] = useState(false);

  const say = (k: string, v: string) => setNotes((p) => ({ ...p, [k]: v }));
  const toggle = (k: StepKey) =>
    setOpen((p) => {
      const n = new Set(p);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });

  const guarded = async (k: string, fn: () => Promise<string>) => {
    setBusy(k);
    say(k, '');
    try {
      say(k, await fn());
    } catch (e) {
      say(k, e instanceof Error ? e.message : 'Failed');
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const runCheck = () =>
    guarded('check', async () => {
      const r = (await api.runWatchdog()) as { ok?: boolean; observed?: number; busy?: boolean };
      return r.busy ? 'A check is already running.' : `done: ${r.observed ?? 0} findings`;
    });
  const runDigest = () =>
    guarded('digest', async () => {
      const r = await api.runDigest(false);
      return `digest #${r.id} built (${r.status})`;
    });
  const runQwen = () =>
    guarded('qwen', async () => {
      const r = await api.runReadout();
      return `read-out: ${r.status}${r.detail ? ` (${r.detail})` : ''}`;
    });
  const runHaiku = () =>
    guarded('haiku', async () => {
      const r = await api.runTriage();
      return `triage: ${r.status}${r.estimated_cost_usd !== undefined ? ` (would cost about ${usd(r.estimated_cost_usd)})` : ''}`;
    });
  const runAll = async () => {
    await runCheck();
    await runDigest();
    await runQwen();
    await runHaiku();
    await settings.reload();
  };

  const RANK: Record<EscalationMode, number> = { quick: 1, investigate: 2, deep: 3 };
  const [redo, setRedo] = useState(false);
  const selectedAll = plan?.tasks.filter((t) => t.selected) ?? [];
  // a ticked task is skipped only when it already has a current proposal of this
  // mode or a deeper one (so Investigate still picks up a task that only has Propose)
  const ticked = selectedAll.filter(
    (t) => redo || !t.proposal || t.proposal_stale || RANK[t.proposal.mode ?? 'quick'] < RANK[mode],
  );
  const estOf = (t: Task) => (mode === 'quick' ? t.estimate : mode === 'investigate' ? t.estimate_investigate : t.estimate_deep);
  const total = ticked.reduce((a, t) => a + (estOf(t).usd ?? 0), 0);
  const runSonnet = () =>
    guarded('sonnet', async () => {
      setConfirm(false);
      setOpen((p) => new Set(p).add('sonnet'));
      let ok = 0;
      let last = '';
      for (const t of ticked) {
        const r = await api.proposal(t.id, mode);
        if (r.status === 'ok') ok += 1;
        else last = r.status;
        if (r.status === 'skipped_budget' || r.status === 'skipped_no_key') break;
      }
      if (last === 'dry_run')
        return `Dry-run is ON, so Sonnet was NOT called and no proposal was written (${ticked.length} tasks checked). If it were live it would cost up to ${usd(total)}. Switch dry-run off in Settings to get real proposals.`;
      if (last === 'skipped_no_key') return 'No Anthropic key found in the Keychain (jarvis-the-janitor).';
      if (last === 'skipped_budget') return 'The monthly budget guard stopped the call.';
      return `${ok} of ${ticked.length} proposals written${last ? ` (last: ${last})` : ''}`;
    });

  const requestApproval = (clusterId: string) =>
    guarded('exec', async () => {
      const r = await api.requestExecution(clusterId);
      await execs.reload();
      if (r.status === 'awaiting_approval') return `Sent to Telegram (request #${r.id}). Approve it there.`;
      if (r.status === 'not_executable') return `Cannot be executed: ${(r.problems ?? []).join('; ')}`;
      return r.status.replace(/_/g, ' ');
    });

  const setGroup = async (group: 'telegram' | 'executor' | 'qwen', next: Record<string, unknown>) => {
    await api.putSettings({ [group]: next });
    await Promise.all([settings.reload(), execs.reload(), tg.reload()]);
  };

  const toggleAuto = async () => {
    const w = settings.data?.watchdog;
    if (!w) return;
    await api.setWatchdog({ ...w, enabled: !w.enabled, interval_min: !w.enabled ? 1440 : w.interval_min });
    await Promise.all([settings.reload(), reload()]);
  };

  if (!data) return <Status error={null} />;
  const t: Team = data;
  const auto = settings.data?.watchdog.enabled ?? t.l0.enabled ?? false;
  const risky = (r: string) => (r === 'high' ? 'var(--color-error)' : r === 'medium' ? 'var(--color-warning)' : 'var(--color-success)');
  const btn = { background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' } as const;
  const run = (k: string, label: string, fn: () => void, extra?: string) => (
    <button disabled={busy !== null} onClick={fn} className="px-2 py-1 rounded text-[11px] cursor-pointer shrink-0" style={btn} title={extra}>
      {busy === k ? 'Working…' : label}
    </button>
  );
  const note = (k: string) =>
    notes[k] ? (
      <div className="text-[11px]" style={muted}>
        {notes[k]}
      </div>
    ) : null;
  const lastTriage = t.l2.runs[0];

  return (
    <div className="flex flex-col gap-3">
      <div className="hud-panel p-3 flex flex-col gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="text-xs flex-1 min-w-[260px]" style={muted}>
            Pilot mode: nothing runs by itself. You trigger each step here and review what each layer produced.{' '}
            {t.dry_run ? 'Dry-run is ON: Haiku and Sonnet send nothing and cost nothing.' : 'Dry-run is OFF: Haiku and Sonnet calls are paid.'}
          </div>
          <label className="flex items-center gap-2 text-xs cursor-pointer" style={muted} title="Off = pilot (manual). On = one automatic run per day.">
            <input type="checkbox" checked={auto} onChange={() => void toggleAuto()} />
            Automatic daily run
          </label>
          {run('all', 'Run steps 1 to 4', () => void runAll(), 'Check, digest, Qwen read-out, Haiku triage. Never Sonnet.')}
        </div>
        <div className="text-[11px]" style={faint}>
          {auto ? `Automatic: one run every ${Math.round((t.l0.interval_min ?? 1440) / 60)} h (digest at the schedule time).` : 'Automatic runs are OFF.'}
        </div>
      </div>

      <Step
        n={1}
        tag="Python"
        title="Check Home Assistant"
        role="Read-only: issues, backups, integrations, entities. Free, no model."
        status={t.l0.last_tick ? `${ago(t.l0.last_tick.started)} ago · ${t.l0.last_tick.observed} findings` : 'not run yet'}
        open={open.has('check')}
        onToggle={() => toggle('check')}
        action={run('check', 'Run check', () => void runCheck())}
      >
        {note('check')}
        {t.l0.last_tick ? (
          <div className="text-xs" style={muted}>
            Took {t.l0.last_tick.duration_s}s · {t.l0.last_tick.new.length} new · {t.l0.last_tick.resolved.length} resolved
            {Object.keys(t.l0.last_tick.errors).length ? ` · could not read: ${Object.keys(t.l0.last_tick.errors).join(', ')}` : ''}
            {t.l0.issues ? ` · ${t.l0.issues.active} open issues` : ''}
          </div>
        ) : (
          <div className="text-xs" style={faint}>No check yet.</div>
        )}
      </Step>

      <Step
        n={2}
        tag="Python"
        title="Build the digest"
        role="Deterministic text from real data. No model writes it."
        status={t.l0.report ? `#${t.l0.report.id} · ${ago(t.l0.report.created_at)} ago · ${t.l0.report.status}` : 'not built yet'}
        open={open.has('digest')}
        onToggle={() => toggle('digest')}
        action={run('digest', 'Build digest', () => void runDigest())}
      >
        {note('digest')}
        {t.l0.report ? (
          <pre className="text-xs whitespace-pre-wrap hud-mono" style={{ color: 'var(--color-text)' }}>
            {t.l0.report.text}
          </pre>
        ) : (
          <div className="text-xs" style={faint}>No digest yet.</div>
        )}
      </Step>

      <Step
        n={3}
        tag="Qwen"
        title="Read-out"
        role="Local and free. Reads the digest aloud; every number is checked against it."
        status={t.l1.latest ? `${ago(t.l1.latest.created_at)} ago · ${t.l1.latest.guard_ok ? 'numbers verified' : 'fallback used'}` : 'not run yet'}
        open={open.has('qwen')}
        onToggle={() => toggle('qwen')}
        action={run('qwen', 'Run read-out', () => void runQwen())}
      >
        {note('qwen')}
        <div className="flex items-center gap-2 flex-wrap">
          <button
            disabled={speech.busy}
            onClick={() => (speech.speaking ? speech.stop() : void speech.run())}
            className="px-2 py-1 rounded text-[11px] cursor-pointer flex items-center gap-1"
            style={{ ...btn, border: '1px solid var(--color-accent)' }}
          >
            {speech.speaking ? <Square size={11} /> : <Volume2 size={11} />} {speech.speaking ? 'Stop' : 'Read aloud (and show in chat)'}
          </button>
          {speech.error && <span className="text-[11px]" style={{ color: 'var(--color-warning)' }}>{speech.error}</span>}
        </div>
        {settings.data && (
          <div className="flex flex-col gap-1">
            {(
              [
                ['house_status', 'Jarvis answers questions about the house (from the status file)'],
                ['read_aloud', 'The "read aloud" buttons are available'],
              ] as ['house_status' | 'read_aloud', string][]
            ).map(([k, label]) => (
              <label key={k} className="flex items-center gap-2 text-xs cursor-pointer" style={muted}>
                <input
                  type="checkbox"
                  checked={settings.data!.qwen[k]}
                  onChange={() => void setGroup('qwen', { ...settings.data!.qwen, [k]: !settings.data!.qwen[k] })}
                />
                {label}
              </label>
            ))}
          </div>
        )}
        {house.data && (
          <details className="text-xs" style={muted}>
            <summary className="cursor-pointer">The plain-English status Qwen reads (file: {house.data.path})</summary>
            <pre className="whitespace-pre-wrap hud-mono mt-1" style={{ color: 'var(--color-text)' }}>{house.data.text}</pre>
          </details>
        )}
        {t.l1.latest ? (
          <>
            <div className="text-xs" style={{ color: 'var(--color-text)' }}>{t.l1.latest.text}</div>
            <div className="text-[11px] hud-mono" style={faint}>
              {t.l1.latest.model} · {t.l1.latest.tokens_in} in / {t.l1.latest.tokens_out} out · {t.l1.latest.seconds.toFixed(1)}s
            </div>
            {t.l1.latest.note && <div className="text-[11px]" style={{ color: 'var(--color-warning)' }}>{t.l1.latest.note}</div>}
          </>
        ) : (
          <div className="text-xs" style={faint}>No read-out yet.</div>
        )}
      </Step>

      <Step
        n={4}
        tag="Haiku"
        title={`Triage notes · ${t.l2.model}`}
        role="A short note, suspected cause and effort per cluster. Never diagnoses."
        status={lastTriage ? `${ago(lastTriage.started)} ago · ${lastTriage.outcome}` : t.dry_run ? 'dry-run' : 'not run yet'}
        open={open.has('haiku')}
        onToggle={() => toggle('haiku')}
        action={run('haiku', t.dry_run ? 'Estimate' : 'Run triage', () => void runHaiku())}
      >
        {note('haiku')}
        {t.l2.reasoning.length === 0 ? (
          <div className="text-xs" style={faint}>No notes stored yet (dry-run, or nothing new to triage).</div>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {t.l2.reasoning.map((r) => (
              <li key={r.cluster_id} className="text-xs">
                <span style={{ color: 'var(--color-text)' }}>{r.label}</span>
                {r.effort && <span className="ml-2"><Chip text={r.effort} /></span>}
                <div style={muted}>
                  {r.note} {r.cause ? `· likely cause: ${r.cause}` : ''}
                </div>
              </li>
            ))}
          </ul>
        )}
        <Runs runs={t.l2.runs} />
      </Step>

      <Step
        n={5}
        tag="You"
        title="Choose the tasks"
        role="Tick the tasks you want planned. Quick wins first."
        status={plan ? `${plan.tasks.length} tasks · ${plan.tasks.filter((x) => x.selected).length} ticked` : '—'}
        open={false}
        onToggle={() => undefined}
        action={
          <button onClick={() => navigate('/maintenance?tab=plan')} className="px-2 py-1 rounded text-[11px] cursor-pointer shrink-0" style={btn}>
            Open the plan
          </button>
        }
      >
        <span />
      </Step>

      <Step
        n={6}
        tag="Sonnet"
        title={`Proposals · ${t.l3.model}`}
        role="Plans the ticked tasks and writes the instruction for the executor. Only when you start it."
        status={`${t.l3.escalated.length} proposals${t.dry_run ? ' · dry-run' : ''}`}
        open={open.has('sonnet')}
        onToggle={() => toggle('sonnet')}
      >
        {t.dry_run && (
          <div
            className="text-xs p-2 rounded-md flex items-center gap-3 flex-wrap"
            style={{ background: 'color-mix(in srgb, var(--color-warning) 12%, transparent)', color: 'var(--color-text)' }}
          >
            <span className="flex-1 min-w-[240px]">
              Dry-run is ON: Sonnet is not called, so no proposal can be written. The buttons only show what it would cost.
            </span>
            <button onClick={() => navigate('/maintenance?tab=settings')} className="px-2 py-1 rounded text-[11px] cursor-pointer shrink-0" style={btn}>
              Open Settings
            </button>
          </div>
        )}
        <div className="flex items-center gap-2 flex-wrap">
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as EscalationMode)}
            className="text-[11px] rounded px-1 py-1 cursor-pointer"
            style={btn}
            aria-label="Escalation mode"
          >
            <option value="quick">Propose (Sonnet)</option>
            <option value="investigate">Investigate (Sonnet, read-only lookups)</option>
            <option value="deep">Deep (Opus)</option>
          </select>
          {!confirm ? (
            <button
              disabled={busy !== null || ticked.length === 0}
              onClick={() => setConfirm(true)}
              className="px-2 py-1 rounded text-[11px] cursor-pointer"
              style={btn}
            >
              {t.dry_run ? 'Estimate' : 'Run'} {mode === 'quick' ? 'Propose' : mode === 'investigate' ? 'Investigate' : 'Deep'} on {ticked.length} of {selectedAll.length} ticked task{selectedAll.length === 1 ? '' : 's'}
            </button>
          ) : (
            <>
              <span className="text-[11px]" style={muted}>
                {t.dry_run ? 'Dry-run: nothing is sent.' : `Up to ${usd(total)} will be spent.`} Start?
              </span>
              <button onClick={() => void runSonnet()} className="px-2 py-1 rounded text-[11px] cursor-pointer" style={{ ...btn, border: '1px solid var(--color-accent)' }}>
                Yes, start
              </button>
              <button onClick={() => setConfirm(false)} className="px-2 py-1 rounded text-[11px] cursor-pointer" style={btn}>
                Cancel
              </button>
            </>
          )}
          <label className="flex items-center gap-1 text-[11px]" style={faint} title="Run again even if the task already has a proposal of this mode">
            <input type="checkbox" checked={redo} onChange={(e) => setRedo(e.target.checked)} />
            redo existing
          </label>
          {ticked.length === 0 && (
            <span className="text-[11px]" style={faint}>
              {selectedAll.length === 0
                ? 'Tick tasks in the Plan tab first.'
                : `The ${selectedAll.length} ticked task${selectedAll.length === 1 ? ' already has' : 's already have'} a proposal of this mode or deeper. Pick a deeper mode, or tick "redo existing".`}
            </span>
          )}
        </div>
        {note('sonnet')}
        {t.l3.escalated.length === 0 ? (
          <div className="text-xs" style={faint}>Nothing escalated yet.</div>
        ) : (
          t.l3.escalated.map((p) => (
            <div key={p.cluster_id} className="flex flex-col gap-1.5 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
              <div className="flex items-center gap-2 text-xs flex-wrap">
                <span className="font-medium" style={{ color: 'var(--color-text)' }}>{p.label}</span>
                <Chip text={`risk ${p.risk}`} color={risky(p.risk)} />
                {!p.current && <Chip text="out of date" color="var(--color-warning)" />}
                <span className="hud-mono" style={faint}>{p.proposal.mode ?? 'quick'} · {usd(p.cost_usd)}</span>
                {p.current && (p.proposal.instruction?.steps.length ?? 0) > 0 && (
                  <button
                    disabled={busy !== null || (execs.data?.executions ?? []).some((e) => e.cluster_id === p.cluster_id && ['awaiting_approval', 'approved', 'running'].includes(e.status))}
                    onClick={() => void requestApproval(p.cluster_id)}
                    className="ml-auto px-2 py-1 rounded text-[11px] cursor-pointer"
                    style={{ ...btn, border: '1px solid var(--color-accent)' }}
                    title="Sends the steps to your Janitor bot. Nothing happens until you press Approve in Telegram."
                  >
                    Send for approval in Telegram
                  </button>
                )}
              </div>
              <ProposalView p={p.proposal} />
            </div>
          ))
        )}
        <Runs runs={t.l3.runs} />
        {t.l4.runs.length > 0 && (
          <>
            <div className="hud-label">Opus runs</div>
            <Runs runs={t.l4.runs} />
          </>
        )}
      </Step>

      <Step
        n={7}
        tag="Haiku"
        title="Approval and execution"
        role="You approve in Telegram; Haiku then runs exactly the approved steps, nothing else."
        status={`${(execs.data?.executions ?? []).filter((e) => e.status === 'awaiting_approval').length} waiting · ${execs.data?.executor.live ? 'LIVE' : 'simulation'}`}
        open={open.has('exec')}
        onToggle={() => toggle('exec')}
      >
        {note('exec')}
        <div className="text-xs" style={muted}>
          Telegram bot:{' '}
          {tg.data?.running ? `connected as @${tg.data.username}` : `not running${tg.data?.last_error ? ` (${tg.data.last_error})` : ''}`}
          {' · '}locked to your Telegram user only.
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button disabled={busy !== null} className="px-2 py-1 rounded text-[11px] cursor-pointer" style={btn}
            onClick={() => void guarded('exec', async () => { await api.telegramTest(); return 'Test message sent to Telegram.'; })}>
            Send test message
          </button>
          <button disabled={busy !== null} className="px-2 py-1 rounded text-[11px] cursor-pointer" style={btn}
            onClick={() => void guarded('exec', async () => { const r = await api.telegramDigest(); return `Digest: ${r.status}`; })}>
            Send digest to Telegram
          </button>
        </div>
        {settings.data && (
          <div className="flex flex-col gap-1.5 pt-1">
            {(
              [
                ['telegram', 'enabled', 'Telegram bot on'],
                ['telegram', 'digest', 'Send the digest to Telegram'],
                ['telegram', 'alerts', 'Alerts for new critical issues (never during quiet hours)'],
                ['executor', 'require_backup', 'Live runs need a backup newer than the limit'],
              ] as ['telegram' | 'executor', string, string][]
            ).map(([g, k, label]) => {
              const cfg = settings.data![g] as unknown as Record<string, boolean>;
              return (
                <label key={`${g}.${k}`} className="flex items-center gap-2 text-xs cursor-pointer" style={muted}>
                  <input type="checkbox" checked={cfg[k]} onChange={() => void setGroup(g, { ...settings.data![g], [k]: !cfg[k] })} />
                  {label}
                </label>
              );
            })}
            <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: settings.data.executor.live ? 'var(--color-error)' : 'var(--color-text-secondary)' }}>
              <input
                type="checkbox"
                checked={settings.data.executor.live}
                onChange={() => {
                  const live = !settings.data!.executor.live;
                  if (live && !window.confirm('LIVE mode lets approved steps change Home Assistant. Home Assistant will still ask for its own approval. Switch on?')) return;
                  void setGroup('executor', { ...settings.data!.executor, live });
                }}
              />
              Executor LIVE (off = simulation: the steps are logged, nothing is sent)
            </label>
            <label className="flex items-center gap-2 text-xs" style={muted}>
              Operator
              <select
                value={settings.data.executor.operator}
                onChange={(e) => void setGroup('executor', { ...settings.data!.executor, operator: e.target.value })}
                className="text-[11px] rounded px-1 py-0.5 cursor-pointer"
                style={btn}
              >
                <option value="haiku">Haiku (follows the approved steps only)</option>
                <option value="python">Python (no model)</option>
              </select>
            </label>
          </div>
        )}
        <div className="hud-label pt-2">Requests</div>
        {(execs.data?.executions ?? []).length === 0 ? (
          <div className="text-xs" style={faint}>No requests yet. Use "Send for approval in Telegram" on a proposal in step 6.</div>
        ) : (
          (execs.data?.executions ?? []).map((e: Execution) => (
            <div key={e.id} className="flex flex-col gap-1 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
              <div className="flex items-center gap-2 text-xs flex-wrap">
                <span className="hud-mono" style={faint}>#{e.id}</span>
                <span className="font-medium" style={{ color: 'var(--color-text)' }}>{e.label}</span>
                <Chip text={e.status.replace('_', ' ')} color={['failed', 'rolled_back'].includes(e.status) ? 'var(--color-error)' : e.status === 'done' ? 'var(--color-success)' : undefined} />
                <Chip text={e.live ? 'live' : 'simulation'} />
                <span style={faint}>{ago(e.created_at)} ago</span>
                {e.status === 'awaiting_approval' && (
                  <button className="ml-auto px-2 py-1 rounded text-[11px] cursor-pointer" style={btn}
                    onClick={() => void guarded('exec', async () => { await api.cancelExecution(e.id); await execs.reload(); return `#${e.id} withdrawn`; })}>
                    Withdraw
                  </button>
                )}
              </div>
              <ul className="text-[11px] hud-mono flex flex-col gap-0.5" style={faint}>
                {e.log.slice(-8).map((l, i) => (
                  <li key={i} className="truncate" title={l.detail}>
                    {new Date(l.t * 1000).toLocaleTimeString()} {l.event}{l.detail ? ` · ${l.detail}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </Step>

      <div className="hud-panel p-3">
        <div className="hud-label mb-1">This month (paid)</div>
        {t.month.length === 0 ? (
          <div className="text-xs" style={faint}>No paid calls yet.</div>
        ) : (
          t.month.map((m) => (
            <div key={m.model} className="flex justify-between text-xs py-0.5" style={muted}>
              <span>{m.model} · {m.calls} calls</span>
              <span className="hud-mono">{tokens(m.tokens_in)} in · {tokens(m.tokens_out)} out · {usd(m.cost_usd)}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// --- Reports ---------------------------------------------------------------------

function ReportsTab() {
  const { data, reload } = usePolled(api.latestReport, 60_000);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const r = data?.report;
  const run = async () => {
    setBusy(true);
    setNote(null);
    try {
      const out = await api.runReport();
      setNote(`Triage: ${out.triage?.status ?? 'n/a'}`);
      await reload();
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <button
          onClick={() => void run()}
          disabled={busy}
          className="px-3 py-1.5 rounded-lg text-xs cursor-pointer flex items-center gap-1.5"
          style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
        >
          <RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> Build digest now
        </button>
        {note && (
          <span className="text-xs" style={muted}>
            {note}
          </span>
        )}
      </div>
      {r ? (
        <div className="hud-panel p-3">
          <div className="hud-label mb-2">
            Latest digest · {new Date(r.created_at * 1000).toLocaleString()} · {r.status}
          </div>
          <pre className="text-xs whitespace-pre-wrap hud-mono" style={{ color: 'var(--color-text)' }}>
            {r.summary_text}
          </pre>
        </div>
      ) : (
        <div className="text-sm" style={faint}>
          No digest yet. One is built at the scheduled time (daily 07:30 in the pilot) or with the button above.
        </div>
      )}
      <div className="text-[11px]" style={faint}>
        Telegram delivery arrives with the Jarvis the Janitor bot. Until then the digest lives here.
      </div>
    </div>
  );
}

// --- Page ------------------------------------------------------------------------

export function MaintenancePage() {
  const [params, setParams] = useSearchParams();
  const rawTab = params.get('tab') === 'agents' ? 'flow' : params.get('tab');
  const tab: Tab = (TABS.find((t) => t.id === rawTab)?.id ?? 'dashboard') as Tab;
  const section = params.get('section');
  const overview = usePolled(api.overview, 60_000);
  const plan = usePolled(api.plan, 60_000);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    if (tab === 'dashboard' && section && overview.data) {
      document.getElementById(section)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [tab, section, overview.data]);

  const checkNow = async () => {
    setChecking(true);
    try {
      await api.runWatchdog();
      await Promise.all([overview.reload(), plan.reload()]);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto px-6 py-8">
      <div className="max-w-5xl mx-auto w-full flex flex-col gap-4">
        <header className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold" style={{ color: 'var(--color-text)' }}>
              Maintenance
            </h1>
            <p className="text-xs mt-1" style={muted}>
              Jarvis the Janitor · read-only. It watches Home Assistant and plans; it changes nothing by itself.
            </p>
          </div>
          <button
            onClick={() => void checkNow()}
            disabled={checking}
            className="px-3 py-1.5 rounded-lg text-xs cursor-pointer flex items-center gap-1.5"
            style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
          >
            <RefreshCw size={12} className={checking ? 'animate-spin' : ''} /> Check now
          </button>
        </header>

        <nav className="flex gap-1" style={{ borderBottom: '1px solid var(--color-border)' }}>
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setParams(t.id === 'dashboard' ? {} : { tab: t.id })}
              className="px-3 py-2 text-sm cursor-pointer"
              style={{
                color: tab === t.id ? 'var(--color-text)' : 'var(--color-text-secondary)',
                borderBottom: `2px solid ${tab === t.id ? 'var(--color-accent)' : 'transparent'}`,
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {tab === 'dashboard' &&
          (overview.data ? (
            <DashboardTab o={overview.data} initial={section} key={section ?? 'none'} />
          ) : (
            <Status error={overview.error} />
          ))}
        {tab === 'plan' &&
          (plan.data ? <PlanTab plan={plan.data} reload={() => void plan.reload()} /> : <Status error={plan.error} />)}
        {tab === 'flow' && <FlowTab plan={plan.data} />}
        {tab === 'reports' && <ReportsTab />}
        {tab === 'settings' && <SettingsTab />}
      </div>
    </div>
  );
}

function Status({ error }: { error: string | null }) {
  return (
    <div className="text-sm" style={faint}>
      {error ? `Janitor not reachable: ${error}` : 'Loading…'}
    </div>
  );
}
