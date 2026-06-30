'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { knowledgeApi } from '@/lib/api';
import { clsx } from 'clsx';
import { CheckCircle, XCircle } from 'lucide-react';

type DraftStatus = 'draft' | 'approved' | 'rejected';

export function KnowledgeReview() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<DraftStatus>('draft');
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);

  const draftsQuery = useQuery({
    queryKey: ['knowledge', 'drafts', status, page],
    queryFn: async () => (await knowledgeApi.listDrafts({ status, page, limit: 50 })).data,
  });

  const drafts = draftsQuery.data?.data || [];

  const draftDetailQuery = useQuery({
    queryKey: ['knowledge', 'draft', selectedDraftId],
    queryFn: async () => (await knowledgeApi.getDraft(selectedDraftId!)).data,
    enabled: Boolean(selectedDraftId),
  });

  const draft = draftDetailQuery.data;

  const [title, setTitle] = useState('');
  const [problemSummary, setProblemSummary] = useState('');
  const [solutionSteps, setSolutionSteps] = useState<string>('');
  const [preconditions, setPreconditions] = useState<string>('');
  const [tags, setTags] = useState<string>('');
  const [topic, setTopic] = useState<string>('');
  const [confidence, setConfidence] = useState<string>('');

  useEffect(() => {
    if (!draft) return;
    setTitle(String(draft.title || ''));
    setProblemSummary(String(draft.problemSummary || ''));
    setSolutionSteps(Array.isArray(draft.solutionSteps) ? draft.solutionSteps.join('\n') : '');
    setPreconditions(Array.isArray(draft.preconditions) ? draft.preconditions.join('\n') : '');
    setTags(Array.isArray(draft.tags) ? draft.tags.join(', ') : '');
    setTopic(String(draft.topic || ''));
    setConfidence(draft.confidence !== null && draft.confidence !== undefined ? String(draft.confidence) : '');
  }, [draft]);

  const updateMutation = useMutation({
    mutationFn: async () =>
      knowledgeApi.updateDraft(selectedDraftId!, {
        title: title.trim() || undefined,
        problemSummary: problemSummary.trim() || undefined,
        solutionSteps: splitLines(solutionSteps),
        preconditions: splitLines(preconditions),
        tags: splitTags(tags),
        topic: topic.trim() || undefined,
        confidence: confidence.trim() ? Number(confidence) : undefined,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['knowledge', 'drafts'] });
      await queryClient.invalidateQueries({ queryKey: ['knowledge', 'draft', selectedDraftId] });
    },
  });

  const approveMutation = useMutation({
    mutationFn: async () => knowledgeApi.approveDraft(selectedDraftId!),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['knowledge', 'drafts'] });
      await queryClient.invalidateQueries({ queryKey: ['knowledge', 'article'] });
      setSelectedDraftId(null);
    },
  });

  const rejectMutation = useMutation({
    mutationFn: async () => knowledgeApi.rejectDraft(selectedDraftId!, 'Rejected via UI'),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['knowledge', 'drafts'] });
      setSelectedDraftId(null);
    },
  });

  const canEdit = draft?.status === 'draft';

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="card p-6 space-y-4">
        <div>
          <div className="font-medium text-gray-900 dark:text-white">Knowledge Draft Review</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">Drafts require human approval before publishing.</div>
        </div>

        <div className="flex items-center gap-2">
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as DraftStatus);
              setSelectedDraftId(null);
              setPage(1);
            }}
            className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
          >
            <option value="draft">Draft</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
          </select>
          <button className="btn btn-secondary" onClick={() => draftsQuery.refetch()} disabled={draftsQuery.isFetching}>
            Refresh
          </button>
        </div>

        {draftsQuery.isLoading ? (
          <div className="text-gray-500">Loading drafts…</div>
        ) : draftsQuery.error ? (
          <div className="text-red-600">Failed to load drafts</div>
        ) : drafts.length === 0 ? (
          <div className="text-gray-500">No drafts</div>
        ) : (
          <div className="space-y-2">
            {drafts.map((d: any) => (
              <button
                key={d.id}
                type="button"
                onClick={() => setSelectedDraftId(String(d.id))}
                className={clsx(
                  'w-full text-left p-3 rounded-lg border transition-colors',
                  selectedDraftId === d.id
                    ? 'border-primary-500 bg-primary-50 dark:bg-primary-950'
                    : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'
                )}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-gray-900 dark:text-white truncate">{d.title}</div>
                    <div className="text-xs text-gray-500 truncate">
                      {d.topic ? String(d.topic) : 'unknown'} · {Array.isArray(d.tags) ? d.tags.join(', ') : ''}
                    </div>
                  </div>
                  <div className="text-xs text-gray-500">{String(d.status)}</div>
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
          <button className="btn btn-secondary" disabled={drafts.length < 50} onClick={() => setPage((p) => p + 1)}>
            Next
          </button>
        </div>
      </div>

      <div className="card p-6 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-medium text-gray-900 dark:text-white">Draft Editor</div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {draft?.sourceTicketId ? `Ticket: ${draft.sourceTicketId}` : null}
              {draft?.sourceConversationId ? `Conversation: ${draft.sourceConversationId}` : null}
            </div>
          </div>
          <div className="flex gap-2">
            <button className="btn btn-secondary" disabled={!canEdit || updateMutation.isPending} onClick={() => updateMutation.mutate()}>
              Save
            </button>
            <button className="btn btn-primary" disabled={!canEdit || approveMutation.isPending} onClick={() => approveMutation.mutate()}>
              <CheckCircle size={16} className="mr-2" />
              Approve
            </button>
            <button className="btn btn-danger" disabled={!canEdit || rejectMutation.isPending} onClick={() => rejectMutation.mutate()}>
              <XCircle size={16} className="mr-2" />
              Reject
            </button>
          </div>
        </div>

        {!selectedDraftId ? (
          <div className="text-gray-500">Select a draft</div>
        ) : draftDetailQuery.isLoading ? (
          <div className="text-gray-500">Loading…</div>
        ) : draftDetailQuery.error ? (
          <div className="text-red-600">Failed to load draft</div>
        ) : (
          <div className="space-y-3">
            <Field label="Title">
              <input
                value={title}
                disabled={!canEdit}
                onChange={(e) => setTitle(e.target.value)}
                className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
              />
            </Field>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <Field label="Topic">
                <input
                  value={topic}
                  disabled={!canEdit}
                  onChange={(e) => setTopic(e.target.value)}
                  placeholder="billing | onboarding | integrations | bugs | usage"
                  className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
                />
              </Field>
              <Field label="Confidence (0-1)">
                <input
                  value={confidence}
                  disabled={!canEdit}
                  onChange={(e) => setConfidence(e.target.value)}
                  placeholder="0.82"
                  className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
                />
              </Field>
              <Field label="Tags (comma)">
                <input
                  value={tags}
                  disabled={!canEdit}
                  onChange={(e) => setTags(e.target.value)}
                  placeholder="auth, sso, permissions"
                  className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
                />
              </Field>
            </div>
            <Field label="Problem summary">
              <textarea
                value={problemSummary}
                disabled={!canEdit}
                onChange={(e) => setProblemSummary(e.target.value)}
                rows={6}
                className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
              />
            </Field>
            <Field label="Preconditions (one per line)">
              <textarea
                value={preconditions}
                disabled={!canEdit}
                onChange={(e) => setPreconditions(e.target.value)}
                rows={4}
                className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
              />
            </Field>
            <Field label="Solution steps (one per line)">
              <textarea
                value={solutionSteps}
                disabled={!canEdit}
                onChange={(e) => setSolutionSteps(e.target.value)}
                rows={8}
                className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
              />
            </Field>
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
      {children}
    </div>
  );
}

function splitLines(v: string) {
  const out = v
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 50);
  return out.length ? out : undefined;
}

function splitTags(v: string) {
  const out = v
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .slice(0, 50);
  return out.length ? out : undefined;
}
