'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname } from 'next/navigation';
import { MessageCircle, Send, X, Loader2 } from 'lucide-react';
import { clsx } from 'clsx';
import { api } from '@/lib/api';
import { VoiceButton } from '@/components/VoiceButton';

type ChatRole = 'user' | 'assistant';

type ChatMessage = {
  id: string;
  role: ChatRole;
  text: string;
  ts: number;
};

type ChatResponse = {
  conversation_id: string;
  intent: { intent: 'read' | 'write' | 'question'; entity: string; confidence: number };
  message: string;
  suggested_replies: string[];
  action_proposals: any[];
  debug?: any;
};

function newId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function ChatPanel() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [typing, setTyping] = useState(false);
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [suggested, setSuggested] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const clientModule = useMemo(() => pathname || '/', [pathname]);
  // Same-origin proxy: all API calls go through /api/*. No NEXT_PUBLIC_API_URL.
  const intelligencePrefix = '/api';

  useEffect(() => {
    const stored = typeof window !== 'undefined' ? window.localStorage.getItem('crm.chat.conversation_id') : null;
    if (stored) {
      setConversationId(stored);
      return;
    }
    const id = newId();
    setConversationId(id);
    if (typeof window !== 'undefined') window.localStorage.setItem('crm.chat.conversation_id', id);
  }, []);

  useEffect(() => {
    if (!open) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [open, messages, typing]);

  const send = useCallback(
    async (text: string) => {
      const q = String(text || '').trim();
      if (!q || typing) return;

      const userMsg: ChatMessage = { id: newId(), role: 'user', text: q, ts: Date.now() };
      setMessages((prev) => [...prev, userMsg]);
      setInput('');
      setTyping(true);
      setSuggested([]);

      try {
        const resp = await api.post<ChatResponse>(`${intelligencePrefix}/intelligence/query`, {
          query: q,
          module: clientModule,
          conversation_id: conversationId,
          mode: 'chat',
        });
        const data = resp.data;
        if (data?.conversation_id && data.conversation_id !== conversationId) {
          setConversationId(data.conversation_id);
          if (typeof window !== 'undefined') window.localStorage.setItem('crm.chat.conversation_id', data.conversation_id);
        }
        const assistantMsg: ChatMessage = { id: newId(), role: 'assistant', text: data?.message || '', ts: Date.now() };
        setMessages((prev) => [...prev, assistantMsg]);
        setSuggested(Array.isArray(data?.suggested_replies) ? data.suggested_replies : []);
      } catch (e: any) {
        const assistantMsg: ChatMessage = {
          id: newId(),
          role: 'assistant',
          text: 'I couldn’t complete that request. Please try again.',
          ts: Date.now(),
        };
        setMessages((prev) => [...prev, assistantMsg]);
      } finally {
        setTyping(false);
      }
    },
    [typing, intelligencePrefix, clientModule, conversationId]
  );

  const onSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      void send(input);
    },
    [send, input]
  );

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 z-50 rounded-full shadow-lg bg-indigo-600 hover:bg-indigo-700 text-white w-12 h-12 flex items-center justify-center"
        aria-label="Open CRM Copilot"
      >
        <MessageCircle size={20} />
      </button>

      {open && (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/30" onClick={() => setOpen(false)} aria-hidden="true" />
          <div className="absolute bottom-6 right-6 w-[92vw] max-w-md rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-xl overflow-hidden">
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-800">
              <div className="font-medium text-sm text-gray-900 dark:text-white">CRM Copilot</div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
                aria-label="Close"
              >
                <X size={18} className="text-gray-500" />
              </button>
            </div>

            <div ref={scrollRef} className="max-h-[55vh] overflow-auto px-4 py-3 space-y-3">
              {messages.length === 0 && (
                <div className="text-sm text-gray-500">
                  Ask about leads, tickets, customers, or request a change for approval.
                </div>
              )}

              {messages.map((m) => (
                <div
                  key={m.id}
                  className={clsx('text-sm whitespace-pre-wrap', m.role === 'user' ? 'text-right' : 'text-left')}
                >
                  <div
                    className={clsx(
                      'inline-block rounded-2xl px-3 py-2 max-w-[85%]',
                      m.role === 'user'
                        ? 'bg-indigo-600 text-white'
                        : 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white'
                    )}
                  >
                    {m.text}
                  </div>
                </div>
              ))}

              {typing && (
                <div className="text-left text-sm">
                  <div className="inline-flex items-center gap-2 rounded-2xl px-3 py-2 bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-200">
                    <Loader2 size={16} className="animate-spin" />
                    Thinking…
                  </div>
                </div>
              )}
            </div>

            {suggested.length > 0 && (
              <div className="px-4 pb-2 flex flex-wrap gap-2">
                {suggested.slice(0, 3).map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => void send(s)}
                    className="text-xs px-3 py-1.5 rounded-full border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-200"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}

            <form onSubmit={onSubmit} className="border-t border-gray-200 dark:border-gray-800 px-3 py-3">
              <div className="flex items-center gap-2">
                <VoiceButton
                  size="sm"
                  showLanguageBadge={false}
                  onTranscript={(transcript) => {
                    setInput(transcript);
                    void send(transcript);
                  }}
                />
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Ask a question…"
                  className="flex-1 bg-transparent outline-none text-sm text-gray-900 dark:text-white px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700"
                />
                <button
                  type="submit"
                  disabled={!input.trim() || typing}
                  className="px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:hover:bg-indigo-600 text-white flex items-center gap-2"
                >
                  <Send size={16} />
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}

