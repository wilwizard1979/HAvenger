import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { ChevronDown, ChevronRight, RefreshCw, Play } from 'lucide-react';
import {
  api,
  usePolled,
  usd,
  tokens,
  TONE_COLOR,
  SEVERITY_COLOR,
  type Effort,
  type Overview,
  type Plan,
  type Severity,
  type Task,
} from '../lib/maintenance';

type Tab = 'dashboard' | 'plan' | 'reports';

const TABS: { id: Tab; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'plan', label: 'Plan' },
  { id: 'reports', label: 'Reports' },
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
        const value = key === 'last_backup' ? (lb.value === null ? '—' : `${Math.round(lb.value)} h ago`) : (card.value ?? '—');
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
                {sec && sec.total > sec.items.length && (
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

  const requestProposal = () =>
    run(async () => {
      const r = await api.proposal(t.id);
      if (r.status === 'dry_run') setMsg(`Dry-run: would use about ${tokens(t.estimate.tokens_in + t.estimate.tokens_out)} tokens (${usd(t.estimate.usd)}). Nothing was sent.`);
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
            proposal ≈ {tokens(t.estimate.tokens_in + t.estimate.tokens_out)} tok · {usd(t.estimate.usd)}
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
            ) : (
              <button
                disabled={busy}
                onClick={() => void requestProposal()}
                className="text-[11px] px-2 py-1 rounded cursor-pointer flex items-center gap-1"
                style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
                title="Ask Sonnet for a proposed resolution and risk check (read-only, changes nothing)"
              >
                <Play size={10} /> {t.proposal_stale ? 'Refresh proposal' : 'Request proposal'}
              </button>
            )}
          </div>
        </div>
      </div>
      {msg && (
        <div className="text-[11px]" style={muted}>
          {msg}
        </div>
      )}
      {open && t.proposal && (
        <div className="text-xs flex flex-col gap-1.5 pt-2" style={{ borderTop: '1px solid var(--color-border)', color: 'var(--color-text)' }}>
          <div>{t.proposal.summary}</div>
          <div className="flex items-center gap-2">
            <Chip text={`risk ${t.proposal.risk}`} color={t.proposal.risk === 'high' ? 'var(--color-error)' : t.proposal.risk === 'medium' ? 'var(--color-warning)' : 'var(--color-success)'} />
            {t.proposal.needs_user && <Chip text="needs you" />}
          </div>
          <ol className="list-decimal pl-5 space-y-0.5">
            {t.proposal.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
          <div style={muted}>
            <b>Breaking-change check:</b> {t.proposal.breaking_change_check}
          </div>
          <div style={muted}>
            <b>Rollback:</b> {t.proposal.rollback}
          </div>
          {t.proposal.verification.length > 0 && (
            <div style={muted}>
              <b>Verify:</b> {t.proposal.verification.join(' · ')}
            </div>
          )}
          {t.proposal.user_action && (
            <div style={muted}>
              <b>Your action:</b> {t.proposal.user_action}
            </div>
          )}
          <div className="text-[11px]" style={faint}>
            Proposal only. Nothing is applied before the approval step exists and you confirm it.
          </div>
        </div>
      )}
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
  const tab: Tab = (TABS.find((t) => t.id === params.get('tab'))?.id ?? 'dashboard') as Tab;
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
        {tab === 'reports' && <ReportsTab />}
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
