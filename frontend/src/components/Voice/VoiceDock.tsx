import { AudioLines, Square } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router';
import { ParticleOrb } from './ParticleOrb';
import { voiceLevelRef } from './voiceLevel';
import { useAppStore } from '../../lib/store';

/**
 * Bottom-left voice sphere. Solid and still while you type; it starts moving
 * when voice mode is on. The button turns hands-free voice on and off.
 */
export function VoiceDock() {
  const active = useAppStore((s) => s.voiceActive);
  const mode = useAppStore((s) => s.voiceOrbMode);
  const setVoiceActive = useAppStore((s) => s.setVoiceActive);
  const navigate = useNavigate();
  const location = useLocation();

  const toggle = () => {
    // Voice runs inside the chat view, so make sure it is on screen.
    if (!active && location.pathname !== '/') navigate('/');
    setVoiceActive(!active);
  };

  return (
    <div className="px-3 pb-3 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => active && window.dispatchEvent(new Event('jarvis-voice-orb-click'))}
          className="relative shrink-0 rounded-full outline-none"
          style={{ width: 92, height: 92, cursor: active ? 'pointer' : 'default' }}
          title={active ? 'Tap to interrupt or resume' : 'Voice is off'}
          aria-label="Jarvis voice sphere"
        >
          <ParticleOrb levelRef={voiceLevelRef} mode={active ? mode : 'idle'} frozen={!active} />
        </button>
        <div className="flex-1 min-w-0 flex flex-col gap-2">
          <button
            type="button"
            onClick={toggle}
            aria-pressed={active}
            className="flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-xs cursor-pointer transition-colors"
            style={{
              background: active ? 'color-mix(in srgb, var(--color-error) 14%, transparent)' : 'var(--color-accent-subtle)',
              border: `1px solid ${active ? 'var(--color-error)' : 'var(--color-accent)'}`,
              color: 'var(--color-text)',
            }}
          >
            {active ? <Square size={13} /> : <AudioLines size={14} style={{ color: 'var(--color-accent)' }} />}
            {active ? 'Stop voice' : 'Start voice'}
          </button>
          <div id="voice-dock-captions" className="min-h-0" />
        </div>
      </div>
    </div>
  );
}
