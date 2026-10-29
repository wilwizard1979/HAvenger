import { ChatArea } from '../components/Chat/ChatArea';
import { JarvisPanel } from '../components/Ha/JarvisPanel';
import { useAppStore } from '../lib/store';

export function ChatPage() {
  const systemPanelOpen = useAppStore((s) => s.systemPanelOpen);

  return (
    <div className="flex h-full overflow-hidden">
      <div className="flex-1 min-w-0">
        <ChatArea />
      </div>
      {systemPanelOpen && (
        <JarvisPanel onAsk={(prompt) => window.dispatchEvent(new CustomEvent('jarvis-ask', { detail: prompt }))} />
      )}
    </div>
  );
}
