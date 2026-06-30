'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { knowledgeApi } from '@/lib/api';
import { clsx } from 'clsx';
import { FileText } from 'lucide-react';

export function KnowledgeBase({ initialArticleId }: { initialArticleId?: string }) {
  const [page, setPage] = useState(1);
  const [tag, setTag] = useState('');
  const [search, setSearch] = useState('');
  const [selectedArticleId, setSelectedArticleId] = useState<string | null>(initialArticleId || null);

  useEffect(() => {
    if (initialArticleId) setSelectedArticleId(initialArticleId);
  }, [initialArticleId]);

  const listQuery = useQuery({
    queryKey: ['knowledge', 'articles', page, tag],
    queryFn: async () => (await knowledgeApi.listArticles({ page, limit: 50, tag: tag || undefined })).data,
  });
  const [listTimedOut, setListTimedOut] = useState(false);
  useEffect(() => {
    if (listQuery.isLoading) {
      const id = setTimeout(() => setListTimedOut(true), 10000);
      return () => clearTimeout(id);
    }
    setListTimedOut(false);
  }, [listQuery.isLoading]);

  const articles = useMemo(() => listQuery.data?.data || [], [listQuery.data]);
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return articles;
    return articles.filter((a: any) => String(a.title || '').toLowerCase().includes(q));
  }, [articles, search]);

  const articleQuery = useQuery({
    queryKey: ['knowledge', 'article', selectedArticleId],
    queryFn: async () => (await knowledgeApi.getArticle(selectedArticleId!)).data,
    enabled: Boolean(selectedArticleId),
  });
  const [articleTimedOut, setArticleTimedOut] = useState(false);
  useEffect(() => {
    if (articleQuery.isLoading) {
      const id = setTimeout(() => setArticleTimedOut(true), 10000);
      return () => clearTimeout(id);
    }
    setArticleTimedOut(false);
  }, [articleQuery.isLoading]);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="card p-6 space-y-4">
        <div>
          <div className="font-medium text-gray-900 dark:text-white">Knowledge Base</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">Approved, tenant-scoped articles. Reuse is tracked.</div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <div className="text-xs text-gray-500 dark:text-gray-400">Search title</div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="type to filter"
              className="mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
            />
          </div>
          <div>
            <div className="text-xs text-gray-500 dark:text-gray-400">Tag</div>
            <input
              value={tag}
              onChange={(e) => {
                setTag(e.target.value);
                setPage(1);
              }}
              placeholder="optional tag filter"
              className="mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
            />
          </div>
        </div>

        {listQuery.isLoading ? (
          <div className="text-gray-500">Loading articles…</div>
        ) : listTimedOut || listQuery.error ? (
          <div className="text-red-600 space-y-2">
            <div>{listTimedOut ? 'Request timed out' : 'Failed to load articles'}</div>
            <button className="btn btn-secondary btn-sm" onClick={() => { setListTimedOut(false); listQuery.refetch(); }}>
              Retry
            </button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="text-gray-500">No articles found</div>
        ) : (
          <div className="space-y-2">
            {filtered.map((a: any) => (
              <button
                key={a.id}
                type="button"
                onClick={() => setSelectedArticleId(String(a.id))}
                className={clsx(
                  'w-full text-left p-3 rounded-lg border transition-colors flex items-start gap-3',
                  selectedArticleId === a.id
                    ? 'border-primary-500 bg-primary-50 dark:bg-primary-950'
                    : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'
                )}
              >
                <FileText size={18} className="text-indigo-600 mt-0.5" />
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-900 dark:text-white truncate">{a.title}</div>
                  <div className="text-xs text-gray-500 truncate">{Array.isArray(a.tags) ? a.tags.join(', ') : ''}</div>
                </div>
              </button>
            ))}
          </div>
        )}

        <div className="flex items-center justify-between pt-2">
          <button className="btn btn-secondary" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
            Prev
          </button>
          <div className="text-xs text-gray-500">Page {page}</div>
          <button className="btn btn-secondary" disabled={filtered.length < 50} onClick={() => setPage((p) => p + 1)}>
            Next
          </button>
        </div>
      </div>

      <div className="card p-6 space-y-3">
        <div className="font-medium text-gray-900 dark:text-white">Article</div>
        {!selectedArticleId ? (
          <div className="text-gray-500">Select an article</div>
        ) : articleQuery.isLoading ? (
          <div className="text-gray-500">Loading…</div>
        ) : articleTimedOut || articleQuery.error ? (
          <div className="text-red-600 space-y-2">
            <div>{articleTimedOut ? 'Request timed out' : 'Failed to load article'}</div>
            <button className="btn btn-secondary btn-sm" onClick={() => { setArticleTimedOut(false); articleQuery.refetch(); }}>
              Retry
            </button>
          </div>
        ) : articleQuery.data ? (
          <div className="space-y-3">
            <div className="text-lg font-semibold text-gray-900 dark:text-white">{articleQuery.data.title}</div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {Array.isArray(articleQuery.data.tags) ? articleQuery.data.tags.join(' · ') : null}
            </div>
            <pre className="text-sm bg-gray-50 dark:bg-gray-900 p-4 rounded-lg overflow-auto max-h-[540px] whitespace-pre-wrap">
{String(articleQuery.data.content || '')}
            </pre>
          </div>
        ) : (
          <div className="text-gray-500">Not found</div>
        )}
      </div>
    </div>
  );
}
