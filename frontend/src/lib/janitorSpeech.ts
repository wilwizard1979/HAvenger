import { useCallback, useState } from 'react';
import { useTts } from '../hooks/useTts';
import { useAppStore } from './store';
import { api } from './maintenance';

/** Markdown out, plain speech in: headings and bullets are not read out. */
const forSpeech = (t: string) => t.replace(/^#+\s*/gm, '').replace(/^[-*]\s+/gm, '').replace(/\*\*/g, '');

/**
 * "Read the house status aloud": puts the plain-English status into the current
 * chat as an assistant message (so it can be read) and speaks it with the voice
 * Jarvis already uses. No cloud call, nothing in Home Assistant changes.
 */
export function useReadAloud() {
  const { speak, stop, available, state } = useTts();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const hs = await api.houseStatus();
      if (!hs.read_aloud) {
        setError('Read aloud is switched off in the Janitor settings.');
        return;
      }
      const st = useAppStore.getState();
      const cid = st.activeId ?? st.createConversation(st.selectedModel);
      const id = `janitor-${Date.now()}`;
      st.addMessage(cid, { id, role: 'assistant', content: hs.text, timestamp: Date.now() });
      if (available) speak(id, forSpeech(hs.spoken));
      else setError('The voice is not available, the status is in the chat.');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setBusy(false);
    }
  }, [available, speak]);

  return { run, stop, busy, error, speaking: state === 'speaking' || state === 'loading' };
}
