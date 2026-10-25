import { useEffect, useState, type ReactNode } from 'react';
import { getBase } from '../lib/api';
import { api, usePolled, usd, type Effort } from '../lib/maintenance';

// --- types of what the server stores -------------------------------------------

interface S {
  schedule: { frequency: 'daily' | 'weekly'; weekday: string; time: string; timezone: string };
  budget: { monthly_limit_usd: number; pause_auto_escalation_pct: number; hard_stop_pct: number };
  models: { triage: string; diagnose: string; deep: string };
  prices: Record<string, Record<string, number>>;
  prices_verified: { date: string; source: string };
  dry_run: boolean;
  auto_escalation: boolean;
  thresholds: Record<string, number>;
  watchdog: { enabled: boolean; interval_min: number; quiet_hours: { enabled: boolean; start: string; end: string } };
  matrix: { effort_defaults: Record<string, string>; chore_categories: string[]; default_effort: Effort };
  escalation: { max_rounds: number; max_usd: number; opus_max_usd: number };
  qwen: { house_status: boolean; read_aloud: boolean };
  telegram: { enabled: boolean; allowed_user_id: number; digest: boolean; alerts: boolean };
  executor: {
    live: boolean;
    operator: 'haiku' | 'python';
    require_backup: boolean;
    allowed_tools: string[];
    deny_names_regex: string;
    step_timeout_s: number;
  };
  critical_entities: string[];
  motion_sensors: string[];
  ignore_patterns: string[];
  extra_redaction_terms: string[];
}

type Draft = S;

const muted = { color: 'var(--color-text-secondary)' } as const;
const faint = { color: 'var(--color-text-tertiary)' } as const;
const inputStyle = {
  background: 'var(--color-bg-secondary)',
  border: '1px solid var(--color-border)',
  color: 'var(--color-text)',
} as const;

const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const CATEGORIES = ['repair', 'integration', 'log', 'entity_group', 'motion', 'critical_entity', 'battery', 'backup'];
const THRESHOLDS: [string, string, string][] = [
  ['persist_min', 'A problem must last this long before it counts', 'minutes'],
  ['stale_unavailable_days', 'Offline for a long time after', 'days'],
  ['battery_low_pct', 'Battery issue (all devices) at or below', '%'],
  ['battery_warn_pct', 'Motion sensor battery turns orange below', '%'],
  ['backup_age_hours', 'Backup is too old after', 'hours'],
  ['entities_amber_pct', 'Offline devices of a type: warning from', '%'],
  ['entities_red_pct', 'Offline devices of a type: serious from', '%'],
  ['log_error_min', 'Log: errors from one component before it is an issue', 'lines'],
  ['automation_fail_runs_24h', 'Automation failures per day (reserved)', 'runs'],
  ['breaking_change_days', 'Breaking-change warning ahead (reserved)', 'days'],
];

// --- small form pieces ------------------------------------------------------------

function Card({
  title,
  hint,
  keys,
  draft,
  saved,
  onSaved,
  children,
}: {
  title: string;
  hint?: string;
  keys: (keyof S)[];
  draft: Draft;
  saved: S;
  onSaved: () => Promise<void>;
  children: ReactNode;
}) {
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const dirty = keys.some((k) => JSON.stringify(draft[k]) !== JSON.stringify(saved[k]));

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await api.putSettings(Object.fromEntries(keys.map((k) => [k, draft[k]])));
      await onSaved();
      setMsg({ ok: true, text: 'Saved.' });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : 'Could not save' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="hud-panel p-3 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
            {title}
          </div>
          {hint && (
            <div className="text-[11px]" style={faint}>
              {hint}
            </div>
          )}
        </div>
        {msg && (
          <span className="text-[11px]" style={{ color: msg.ok ? 'var(--color-success)' : 'var(--color-error)' }}>
            {msg.text}
          </span>
        )}
        <button
          onClick={() => void save()}
          disabled={!dirty || busy}
          className="px-2.5 py-1 rounded text-[11px] cursor-pointer shrink-0"
          style={{
            ...inputStyle,
            border: `1px solid ${dirty ? 'var(--color-accent)' : 'var(--color-border)'}`,
            opacity: dirty ? 1 : 0.5,
          }}
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
      <div className="flex flex-col gap-2">{children}</div>
    </section>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex items-center gap-3 text-xs">
      <span className="flex-1 min-w-0" style={muted}>
        {label}
        {hint && (
          <span className="block text-[10px]" style={faint}>
            {hint}
          </span>
        )}
      </span>
      {children}
    </label>
  );
}

function Num({ value, onChange, unit, step = 1 }: { value: number; onChange: (v: number) => void; unit?: string; step?: number }) {
  return (
    <span className="flex items-center gap-1.5 shrink-0">
      <input
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : ''}
        onChange={(e) => onChange(e.target.value === '' ? NaN : Number(e.target.value))}
        className="w-24 rounded px-2 py-1 text-right hud-mono text-xs"
        style={inputStyle}
      />
      {unit && (
        <span className="text-[10px] w-12" style={faint}>
          {unit}
        </span>
      )}
    </span>
  );
}

function Text({ value, onChange, width = 'w-44' }: { value: string; onChange: (v: string) => void; width?: string }) {
  return (
    <input value={value} onChange={(e) => onChange(e.target.value)} className={`${width} rounded px-2 py-1 text-xs hud-mono shrink-0`} style={inputStyle} />
  );
}

function Toggle({ value, onChange, danger }: { value: boolean; onChange: (v: boolean) => void; danger?: boolean }) {
  return (
    <input
      type="checkbox"
      checked={value}
      onChange={(e) => onChange(e.target.checked)}
      className="cursor-pointer shrink-0"
      style={danger && value ? { accentColor: 'var(--color-error)' } : undefined}
    />
  );
}

function Pick<T extends string>({ value, options, onChange }: { value: T; options: T[]; onChange: (v: T) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value as T)} className="rounded px-2 py-1 text-xs cursor-pointer shrink-0" style={inputStyle}>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function Lines({
  value,
  onChange,
  rows = 4,
  placeholder,
  commas = false,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  rows?: number;
  placeholder?: string;
  commas?: boolean;
}) {
  return (
    <textarea
      value={value.join('\n')}
      rows={rows}
      placeholder={placeholder}
      onChange={(e) =>
        onChange(
          e.target.value
            .split(commas ? /[\n,]/ : '\n')
            .map((x) => x.trim())
            .filter(Boolean),
        )
      }
      className="w-full rounded px-2 py-1.5 text-xs hud-mono"
      style={inputStyle}
    />
  );
}

// --- critical entities: propose from device classes -----------------------------------

function Proposal({ current, onAdd }: { current: string[]; onAdd: (ids: string[]) => void }) {
  const [rows, setRows] = useState<{ entity_id: string; name: string; kind: string; state: string }[] | null>(null);
  const [pick, setPick] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = (await fetch(`${getBase()}/v1/ha/agents/suggestions`).then((x) => x.json())) as {
        critical: { entity_id: string; name: string; kind: string; state: string }[];
      };
      setRows(r.critical ?? []);
      setPick(new Set((r.critical ?? []).filter((x) => !current.includes(x.entity_id)).map((x) => x.entity_id)));
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not read Home Assistant');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div>
        <button onClick={() => void load()} disabled={busy} className="px-2 py-1 rounded text-[11px] cursor-pointer" style={inputStyle}>
          {busy ? 'Reading Home Assistant…' : 'Propose from device classes'}
        </button>
        {err && (
          <span className="ml-2 text-[11px]" style={{ color: 'var(--color-error)' }}>
            {err}
          </span>
        )}
      </div>
      {rows && (
        <div className="flex flex-col gap-0.5 max-h-56 overflow-y-auto">
          {rows.map((r) => (
            <label key={r.entity_id} className="flex items-center gap-2 text-[11px]" style={muted}>
              <input
                type="checkbox"
                checked={pick.has(r.entity_id)}
                onChange={(e) =>
                  setPick((p) => {
                    const n = new Set(p);
                    if (e.target.checked) n.add(r.entity_id);
                    else n.delete(r.entity_id);
                    return n;
                  })
                }
              />
              <span className="hud-mono">{r.entity_id}</span>
              <span style={faint}>
                {r.kind} · {r.state}
              </span>
            </label>
          ))}
          <div>
            <button
              onClick={() => {
                onAdd([...pick]);
                setRows(null);
              }}
              className="mt-1 px-2 py-1 rounded text-[11px] cursor-pointer"
              style={{ ...inputStyle, border: '1px solid var(--color-accent)' }}
            >
              Add the {pick.size} ticked to the list (then Save)
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// --- the tab -------------------------------------------------------------------------------

export function SettingsTab() {
  const settings = usePolled(api.fullSettings as unknown as () => Promise<S>, 120_000);
  const status = usePolled(api.status, 60_000);
  const [draft, setDraft] = useState<Draft | null>(null);
  const saved = settings.data;

  // adopt the server copy when it first arrives or after a save, keep edits otherwise
  useEffect(() => {
    if (saved && draft === null) setDraft(structuredClone(saved));
  }, [saved, draft]);

  if (!saved || !draft) {
    return (
      <div className="text-sm" style={faint}>
        {settings.error ? `Janitor not reachable: ${settings.error}` : 'Loading…'}
      </div>
    );
  }

  const set = <K extends keyof S>(k: K, v: S[K]) => setDraft({ ...draft, [k]: v });
  const reload = async () => {
    await settings.reload();
    setDraft(null); // re-adopt the saved copy
  };
  const common = { draft, saved, onSaved: reload };
  const s = status.data;
  const confirmed = (text: string, apply: () => void) => {
    if (window.confirm(text)) apply();
  };

  return (
    <div className="flex flex-col gap-3">
      <Card
        {...common}
        title="Master switches"
        hint="Cloud models only spend money when dry-run is OFF. Nothing here changes Home Assistant except executor LIVE."
        keys={['dry_run', 'auto_escalation', 'watchdog', 'executor']}
      >
        <Row label="Cloud dry-run" hint="ON: Haiku and Sonnet send nothing and cost nothing (pilot).">
          <Toggle
            value={draft.dry_run}
            onChange={(v) =>
              v
                ? set('dry_run', true)
                : confirmed('Switch dry-run OFF? Haiku and Sonnet calls will then cost real money (limited by your budget).', () => set('dry_run', false))
            }
          />
        </Row>
        <Row label="Automatic daily run" hint="OFF = pilot: you trigger every step by hand in Flow.">
          <Toggle value={draft.watchdog.enabled} onChange={(v) => set('watchdog', { ...draft.watchdog, enabled: v })} />
        </Row>
        <Row label="Auto-escalation to Sonnet and Opus" hint="Keep OFF: they only run when you press the button.">
          <Toggle value={draft.auto_escalation} onChange={(v) => set('auto_escalation', v)} />
        </Row>
        <Row label="Executor LIVE" hint="OFF = simulation. ON = approved steps change Home Assistant (it still asks its own approval).">
          <Toggle
            danger
            value={draft.executor.live}
            onChange={(v) =>
              v
                ? confirmed('Executor LIVE lets approved steps change Home Assistant. Switch on?', () => set('executor', { ...draft.executor, live: true }))
                : set('executor', { ...draft.executor, live: false })
            }
          />
        </Row>
      </Card>

      <Card {...common} title="Schedule" hint="Report time and how often the automatic run happens." keys={['schedule', 'watchdog']}>
        <Row label="Report">
          <Pick value={draft.schedule.frequency} options={['daily', 'weekly']} onChange={(v) => set('schedule', { ...draft.schedule, frequency: v })} />
        </Row>
        <Row label="Weekday (weekly reports)">
          <Pick value={draft.schedule.weekday} options={WEEKDAYS} onChange={(v) => set('schedule', { ...draft.schedule, weekday: v })} />
        </Row>
        <Row label="Time (24 h)">
          <Text width="w-24" value={draft.schedule.time} onChange={(v) => set('schedule', { ...draft.schedule, time: v })} />
        </Row>
        <Row label="Timezone">
          <Text value={draft.schedule.timezone} onChange={(v) => set('schedule', { ...draft.schedule, timezone: v })} />
        </Row>
        <Row label="Automatic check every" hint="Only used when the automatic run is ON. 1440 = once a day.">
          <Num value={draft.watchdog.interval_min} unit="minutes" onChange={(v) => set('watchdog', { ...draft.watchdog, interval_min: v })} />
        </Row>
        <Row label="Quiet hours" hint="No Telegram messages at all in this window, critical ones included.">
          <Toggle
            value={draft.watchdog.quiet_hours.enabled}
            onChange={(v) => set('watchdog', { ...draft.watchdog, quiet_hours: { ...draft.watchdog.quiet_hours, enabled: v } })}
          />
        </Row>
        <Row label="Quiet from / until">
          <span className="flex gap-2">
            <Text width="w-20" value={draft.watchdog.quiet_hours.start} onChange={(v) => set('watchdog', { ...draft.watchdog, quiet_hours: { ...draft.watchdog.quiet_hours, start: v } })} />
            <Text width="w-20" value={draft.watchdog.quiet_hours.end} onChange={(v) => set('watchdog', { ...draft.watchdog, quiet_hours: { ...draft.watchdog.quiet_hours, end: v } })} />
          </span>
        </Row>
      </Card>

      <Card
        {...common}
        title="Budget and models"
        hint={s ? `This month: ${usd(s.budget.spend_usd)} of ${usd(s.budget.limit_usd)} (${s.budget.pct}%).` : undefined}
        keys={['budget', 'models']}
      >
        <Row label="Monthly limit">
          <Num value={draft.budget.monthly_limit_usd} unit="USD" step={1} onChange={(v) => set('budget', { ...draft.budget, monthly_limit_usd: v })} />
        </Row>
        <Row label="Pause automatic Sonnet and Opus at" hint="Your manual escalations still work until the hard stop.">
          <Num value={draft.budget.pause_auto_escalation_pct} unit="%" onChange={(v) => set('budget', { ...draft.budget, pause_auto_escalation_pct: v })} />
        </Row>
        <Row label="Hard stop at" hint="No cloud call at all after this.">
          <Num value={draft.budget.hard_stop_pct} unit="%" onChange={(v) => set('budget', { ...draft.budget, hard_stop_pct: v })} />
        </Row>
        {(['triage', 'diagnose', 'deep'] as const).map((t) => (
          <Row key={t} label={{ triage: 'Haiku (notes, operator)', diagnose: 'Sonnet (proposals)', deep: 'Opus (deep)' }[t]}>
            <Text value={draft.models[t]} onChange={(v) => set('models', { ...draft.models, [t]: v })} width="w-52" />
          </Row>
        ))}
        <div className="text-[11px]" style={faint}>
          Prices per million tokens (verified {saved.prices_verified.date}):{' '}
          {Object.entries(saved.prices)
            .map(([m, p]) => `${m.replace('claude-', '')} $${p.input}/$${p.output}`)
            .join(' · ')}
        </div>
      </Card>

      <Card {...common} title="Thresholds" hint="When something counts as a problem." keys={['thresholds']}>
        {THRESHOLDS.map(([k, label, unit]) => (
          <Row key={k} label={label}>
            <Num value={draft.thresholds[k] ?? NaN} unit={unit} onChange={(v) => set('thresholds', { ...draft.thresholds, [k]: v })} />
          </Row>
        ))}
      </Card>

      <Card
        {...common}
        title="Lists"
        hint="One entry per line. Wildcards (*) work in the ignore list."
        keys={['critical_entities', 'motion_sensors', 'ignore_patterns', 'extra_redaction_terms']}
      >
        <div className="text-xs" style={muted}>
          Critical entities: always serious when offline (smoke alarms, locks, alarm).
        </div>
        <Lines value={draft.critical_entities} rows={6} onChange={(v) => set('critical_entities', v)} />
        <Proposal current={draft.critical_entities} onAdd={(ids) => set('critical_entities', [...new Set([...draft.critical_entities, ...ids])])} />
        <div className="text-xs pt-1" style={muted}>
          Motion sensors: leave empty for automatic (every sensor with device class motion).
        </div>
        <Lines value={draft.motion_sensors} rows={3} onChange={(v) => set('motion_sensors', v)} />
        <div className="text-xs pt-1" style={muted}>
          Ignore patterns: offline devices that match are not reported.
        </div>
        <Lines value={draft.ignore_patterns} rows={4} onChange={(v) => set('ignore_patterns', v)} />
        <div className="text-xs pt-1" style={muted}>
          Words to hide before anything goes to a cloud model (family names, street, town). Emails, keys and passwords are always hidden.
        </div>
        <Lines
          value={draft.extra_redaction_terms}
          rows={3}
          commas
          placeholder="one per line, or separated by commas. Upper or lower case does not matter"
          onChange={(v) => set('extra_redaction_terms', v)}
        />
        {draft.extra_redaction_terms.length === 0 && (
          <div className="text-[11px]" style={{ color: 'var(--color-warning)' }}>
            The list is empty: names and addresses in Home Assistant data would reach the cloud models in plain text.
          </div>
        )}
      </Card>

      <Card {...common} title="Effort rules" hint="How hard a group of problems is. auto = Haiku rates it." keys={['matrix']}>
        {CATEGORIES.map((c) => (
          <Row key={c} label={c.replace('_', ' ')}>
            <span className="flex items-center gap-3">
              <label className="flex items-center gap-1 text-[11px]" style={faint}>
                <Toggle
                  value={draft.matrix.chore_categories.includes(c)}
                  onChange={(v) =>
                    set('matrix', {
                      ...draft.matrix,
                      chore_categories: v
                        ? [...new Set([...draft.matrix.chore_categories, c])]
                        : draft.matrix.chore_categories.filter((x) => x !== c),
                    })
                  }
                />
                chore
              </label>
              <Pick
                value={draft.matrix.effort_defaults[c] ?? 'auto'}
                options={['auto', 'quick', 'medium', 'hard']}
                onChange={(v) => set('matrix', { ...draft.matrix, effort_defaults: { ...draft.matrix.effort_defaults, [c]: v } })}
              />
            </span>
          </Row>
        ))}
        <Row label="Effort while Haiku has not rated it" hint="Chore = no change in Home Assistant (replace a battery).">
          <Pick value={draft.matrix.default_effort} options={['quick', 'medium', 'hard']} onChange={(v) => set('matrix', { ...draft.matrix, default_effort: v })} />
        </Row>
      </Card>

      <Card {...common} title="Escalation limits" hint="One Sonnet or Opus escalation stops at these limits." keys={['escalation']}>
        <Row label="Read-only lookup rounds">
          <Num value={draft.escalation.max_rounds} onChange={(v) => set('escalation', { ...draft.escalation, max_rounds: v })} />
        </Row>
        <Row label="Sonnet cap per escalation">
          <Num value={draft.escalation.max_usd} unit="USD" step={0.1} onChange={(v) => set('escalation', { ...draft.escalation, max_usd: v })} />
        </Row>
        <Row label="Opus cap per escalation">
          <Num value={draft.escalation.opus_max_usd} unit="USD" step={0.5} onChange={(v) => set('escalation', { ...draft.escalation, opus_max_usd: v })} />
        </Row>
      </Card>

      <Card {...common} title="Telegram and Qwen" keys={['telegram', 'qwen']}>
        <Row label="Telegram bot on">
          <Toggle value={draft.telegram.enabled} onChange={(v) => set('telegram', { ...draft.telegram, enabled: v })} />
        </Row>
        <Row label="Digest to Telegram">
          <Toggle value={draft.telegram.digest} onChange={(v) => set('telegram', { ...draft.telegram, digest: v })} />
        </Row>
        <Row label="Alerts for new serious issues" hint="Never during quiet hours.">
          <Toggle value={draft.telegram.alerts} onChange={(v) => set('telegram', { ...draft.telegram, alerts: v })} />
        </Row>
        <Row label="Jarvis answers questions about the house">
          <Toggle value={draft.qwen.house_status} onChange={(v) => set('qwen', { ...draft.qwen, house_status: v })} />
        </Row>
        <Row label="Read-aloud buttons">
          <Toggle value={draft.qwen.read_aloud} onChange={(v) => set('qwen', { ...draft.qwen, read_aloud: v })} />
        </Row>
      </Card>

      <Card
        {...common}
        title="Executor (advanced)"
        hint="What an approved plan may run. Python checks every step against these rules."
        keys={['executor']}
      >
        <Row label="Operator" hint="Haiku follows the approved steps only; Python needs no model.">
          <Pick value={draft.executor.operator} options={['haiku', 'python']} onChange={(v) => set('executor', { ...draft.executor, operator: v })} />
        </Row>
        <Row label="Live runs need a fresh backup" hint="Newer than the backup limit above.">
          <Toggle value={draft.executor.require_backup} onChange={(v) => set('executor', { ...draft.executor, require_backup: v })} />
        </Row>
        <Row label="Wait for each step" hint="Home Assistant's own approval can take up to 10 minutes.">
          <Num value={draft.executor.step_timeout_s} unit="seconds" onChange={(v) => set('executor', { ...draft.executor, step_timeout_s: v })} />
        </Row>
        <div className="text-xs" style={muted}>
          Allowed tools
        </div>
        <Lines value={draft.executor.allowed_tools} rows={2} onChange={(v) => set('executor', { ...draft.executor, allowed_tools: v })} />
        <Row label="Refuse step names matching (regex)">
          <Text width="w-72" value={draft.executor.deny_names_regex} onChange={(v) => set('executor', { ...draft.executor, deny_names_regex: v })} />
        </Row>
      </Card>
    </div>
  );
}
