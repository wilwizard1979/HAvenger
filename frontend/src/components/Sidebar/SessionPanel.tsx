import { Gauge, HardDrive, Cloud, TrendingDown } from 'lucide-react';
import { useAppStore } from '../../lib/store';

const CLOUD = [
  { name: 'Claude Haiku 5.5', input: 0.1, output: 0.5 },
  { name: 'Claude Sonnet 5.5', input: 2.0, output: 10.0 },
  { name: 'Claude Opus 5.5', input: 4.0, output: 20.0 },
  { name: 'Gemini 3.1 Pro', input: 2.0, output: 12.0 },
];

const money = (n: number) => `$${n.toFixed(n < 0.1 ? 4 : 2)}`;

/** Compact cost savings (kept across restarts): usage, local cost, what the same tokens would cost on cloud models, and the latest measured speed. */
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
    <div className="mx-2 mb-2 p-2.5 rounded-lg text-[0.6875rem]" style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)' }}>
      <div className="hud-label mb-1.5">Cost Savings</div>
      <div className="flex items-center gap-2 mb-1">
        <HardDrive size={13} style={{ color: 'var(--color-success)' }} />
        <span style={{ color: 'var(--color-text-secondary)' }}>
          {savings ? `${savings.total_tokens.toLocaleString()} tokens · ${savings.total_calls} requests` : 'no requests yet'}
        </span>
      </div>
      <div className="flex items-center gap-2 mb-2">
        <span style={{ color: 'var(--color-text-secondary)' }}>Local model cost</span>
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
      <div className="flex items-center gap-2 mt-2">
        <Gauge size={13} style={{ color: 'var(--color-accent)' }} />
        <span style={{ color: 'var(--color-text-secondary)' }}>Last speed</span>
        <span className="ml-auto hud-mono" style={{ color: 'var(--color-text)' }}>
          {tps ? `${tps.toFixed(1)} tok/s` : '—'}
          {ttft != null ? ` · ${(ttft / 1000).toFixed(1)}s first` : ''}
        </span>
      </div>
    </div>
  );
}
