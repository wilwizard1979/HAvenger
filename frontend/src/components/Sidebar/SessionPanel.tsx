import { Gauge, HardDrive, Cloud, TrendingDown } from 'lucide-react';
import { useAppStore } from '../../lib/store';

const CLOUD = [
  { name: 'GPT-5.6 Sol', input: 5.0, output: 30.0 },
  { name: 'Claude Fable 5', input: 10.0, output: 50.0 },
  { name: 'Gemini 3.1 Pro', input: 2.0, output: 12.0 },
];

const money = (n: number) => `$${n.toFixed(n < 0.1 ? 4 : 2)}`;

/** Compact session data: latest measured speed, usage, and local-vs-cloud cost. */
export function SessionPanel() {
  const savings = useAppStore((s) => s.savings);
  const messages = useAppStore((s) => s.messages);

  const last = [...messages].reverse().find((m) => m.role === 'assistant' && m.telemetry?.tokens_per_sec);
  const tps = last?.telemetry?.tokens_per_sec;
  const ttft = last?.telemetry?.ttft_ms;

  const promptK = (savings?.total_prompt_tokens ?? 0) / 1000;
  const completionK = (savings?.total_completion_tokens ?? 0) / 1000;
  const local = savings?.local_cost ?? 0;

  return (
    <div className="mx-2 mb-2 p-2.5 rounded-lg text-[11px]" style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)' }}>
      <div className="hud-label mb-1.5">Session</div>
      <div className="flex items-center gap-2 mb-1.5">
        <Gauge size={13} style={{ color: 'var(--color-accent)' }} />
        <span style={{ color: 'var(--color-text-secondary)' }}>Last speed</span>
        <span className="ml-auto hud-mono" style={{ color: 'var(--color-text)' }}>
          {tps ? `${tps.toFixed(1)} tok/s` : '—'}
          {ttft != null ? ` · ${(ttft / 1000).toFixed(1)}s first` : ''}
        </span>
      </div>
      <div className="flex items-center gap-2 mb-2">
        <HardDrive size={13} style={{ color: 'var(--color-success)' }} />
        <span style={{ color: 'var(--color-text-secondary)' }}>
          {savings ? `${savings.total_calls} req · ${savings.total_tokens.toLocaleString()} tok` : 'no requests yet'}
        </span>
        <span className="ml-auto hud-mono" style={{ color: 'var(--color-success)' }}>
          {money(local)}
        </span>
      </div>
      <div className="flex flex-col gap-1">
        {CLOUD.map((p) => {
          const cost = (promptK * p.input) / 1000 + (completionK * p.output) / 1000;
          return (
            <div key={p.name} className="flex items-center gap-2">
              <Cloud size={12} style={{ color: 'var(--color-text-tertiary)' }} />
              <span className="truncate" style={{ color: 'var(--color-text-secondary)' }}>
                {p.name}
              </span>
              <span className="ml-auto hud-mono" style={{ color: 'var(--color-text)' }}>
                {money(cost)}
              </span>
              {cost > local && (
                <TrendingDown size={11} style={{ color: 'var(--color-success)' }} aria-label="saved vs cloud" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
