'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { Search, FileText, Users, Ticket, Briefcase, CornerDownLeft, Loader2, X } from 'lucide-react';
import { clsx } from 'clsx';
import { api } from '@/lib/api';
import { VoiceButton } from '@/components/VoiceButton';

type SearchResult = {
  entity_type: 'lead' | 'deal' | 'ticket' | 'customer' | string;
  id: string;
  title: string;
  description?: string | null;
  url?: string;
  score?: number;
  sources?: string[];
  score_components?: Record<string, number>;
};

type Suggestion = {
  label: string;
  query: string;
  reason?: string;
};

type SearchResponse = {
  search_id: string;
  intent: any;
  results: SearchResult[];
  suggestions: Suggestion[];
  explainability?: any;
};

function iconForEntity(entityType: string) {
  const t = (entityType || '').toLowerCase();
  if (t === 'lead') return <FileText size={18} className="text-indigo-600" />;
  if (t === 'deal') return <Briefcase size={18} className="text-emerald-600" />;
  if (t === 'ticket') return <Ticket size={18} className="text-amber-600" />;
  if (t === 'customer') return <Users size={18} className="text-sky-600" />;
  return <Search size={18} className="text-zinc-500" />;
}

export function CommandBar() {
  const router = useRouter();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [searchId, setSearchId] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [hasClicked, setHasClicked] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const lastIssuedRef = useRef<string>('');

  const clientModule = useMemo(() => pathname || '/', [pathname]);
  const intelligencePrefix = ''; // API client now normalizes /api/v1

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const isK = e.key.toLowerCase() === 'k';
      if ((e.metaKey || e.ctrlKey) && isK) {
        e.preventDefault();
        setOpen(true);
      }
      if (e.key === 'Escape') {
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    if (!open) return;
    setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  useEffect(() => {
    if (!open) {
      if (searchId && !hasClicked) {
        api.post(`${intelligencePrefix}/intelligence/abandon`, { searchId, query: query.trim() }).catch(() => undefined);
      }
      setLoading(false);
      setResults([]);
      setSuggestions([]);
      setActiveIndex(0);
      setSearchId(null);
      setHasClicked(false);
      lastIssuedRef.current = '';
      return;
    }
  }, [open, hasClicked, query, searchId, intelligencePrefix]);

  useEffect(() => {
    if (!open) return;
    const q = query.trim();
    if (!q) {
      setResults([]);
      setSuggestions([]);
      setSearchId(null);
      setActiveIndex(0);
      return;
    }

    const handle = window.setTimeout(async () => {
      if (lastIssuedRef.current === q) return;
      lastIssuedRef.current = q;
      setLoading(true);
      try {
        const resp = await api.post<SearchResponse>(`${intelligencePrefix}/intelligence/query`, { query: q, module: clientModule });
        setSearchId(resp.data.search_id);
        setResults(resp.data.results || []);
        setSuggestions(resp.data.suggestions || []);
        setActiveIndex(0);
      } catch {
        setResults([]);
        setSuggestions([]);
      } finally {
        setLoading(false);
      }
    }, 160);

    return () => window.clearTimeout(handle);
  }, [open, query, clientModule, intelligencePrefix]);

  const onNavigate = async (r: SearchResult) => {
    if (!searchId) {
      if (r.url) router.push(r.url);
      setOpen(false);
      return;
    }
    setHasClicked(true);
    api.post(`${intelligencePrefix}/intelligence/click`, { searchId, entityType: r.entity_type, entityId: r.id }).catch(() => undefined);
    if (r.url) {
      router.push(r.url);
    } else {
      router.push(`/${r.entity_type}s?id=${encodeURIComponent(r.id)}`);
    }
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, Math.max(0, results.length - 1)));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const r = results[activeIndex];
      if (r) onNavigate(r);
      return;
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="w-full max-w-lg"
        aria-label="Open command bar"
      >
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={20} />
          <div className="input pl-10 w-full text-left flex items-center justify-between">
            <span className={clsx('text-gray-500', query ? 'text-gray-900 dark:text-white' : '')}>
              {query ? query : 'Search anything…'}
            </span>
            <span className="hidden sm:flex items-center gap-1 text-xs text-gray-400">
              <span className="px-2 py-0.5 rounded border border-gray-200 dark:border-gray-700">⌘</span>
              <span className="px-2 py-0.5 rounded border border-gray-200 dark:border-gray-700">K</span>
            </span>
          </div>
        </div>
      </button>

      {open && (
        <div className="fixed inset-0 z-50">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />
          <div className="relative mx-auto mt-24 w-[92vw] max-w-2xl">
            <div className="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-xl overflow-hidden">
              <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-200 dark:border-gray-800">
                <Search size={18} className="text-gray-400" />
                <input
                  ref={inputRef}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={onKeyDown}
                  placeholder="Search leads, deals, tickets, customers…"
                  className="w-full bg-transparent outline-none text-sm text-gray-900 dark:text-white"
                />
                <VoiceButton
                  size="sm"
                  showLanguageBadge={true}
                  onTranscript={(transcript) => setQuery(transcript)}
                />
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
                  aria-label="Close"
                >
                  <X size={18} className="text-gray-500" />
                </button>
              </div>

              <div className="max-h-[55vh] overflow-auto">
                {loading && (
                  <div className="px-4 py-6 text-sm text-gray-500 flex items-center gap-2">
                    <Loader2 size={18} className="animate-spin" />
                    Searching…
                  </div>
                )}

                {!loading && results.length === 0 && query.trim() && (
                  <div className="px-4 py-6 text-sm text-gray-500">No results. Try a different query.</div>
                )}

                {!loading && results.length > 0 && (
                  <div className="py-2">
                    {results.map((r, idx) => {
                      const active = idx === activeIndex;
                      return (
                        <button
                          key={`${r.entity_type}:${r.id}`}
                          type="button"
                          onMouseEnter={() => setActiveIndex(idx)}
                          onClick={() => onNavigate(r)}
                          className={clsx(
                            'w-full px-4 py-3 text-left flex items-start gap-3',
                            active
                              ? 'bg-gray-100 dark:bg-gray-800'
                              : 'hover:bg-gray-50 dark:hover:bg-gray-800/60'
                          )}
                        >
                          <div className="mt-0.5">{iconForEntity(r.entity_type)}</div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center justify-between gap-3">
                              <div className="truncate text-sm font-medium text-gray-900 dark:text-white">
                                {r.title}
                              </div>
                              <div className="flex items-center gap-2 text-xs text-gray-400">
                                {typeof r.score === 'number' && (
                                  <span className="tabular-nums">{r.score.toFixed(2)}</span>
                                )}
                                <CornerDownLeft size={14} />
                              </div>
                            </div>
                            {r.description && (
                              <div className="mt-1 truncate text-xs text-gray-500 dark:text-gray-400">
                                {r.description}
                              </div>
                            )}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

              {(suggestions.length > 0 || !query.trim()) && (
                <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-800">
                  <div className="flex flex-wrap gap-2">
                    {(suggestions.length > 0
                      ? suggestions
                      : [
                          { label: 'Recent leads', query: 'recent leads' },
                          { label: 'Open tickets', query: 'open tickets' },
                          { label: 'Prospecting deals', query: 'prospecting deals' },
                        ])
                      .map((s) => (
                        <button
                          key={s.query}
                          type="button"
                          onClick={() => setQuery(s.query)}
                          className="text-xs px-3 py-1.5 rounded-full border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-200"
                        >
                          {s.label}
                        </button>
                      ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

