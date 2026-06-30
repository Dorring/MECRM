'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle, XCircle, Mail, MessageCircle, ClipboardList, Filter } from 'lucide-react';
import { clsx } from 'clsx';
import { productivityApi, ProductivityProposal } from '@/lib/api';

const priorityStyles: Record<string, string> = {
  high: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  medium: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
  low: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200',
};

function pickDrafts(p: ProductivityProposal) {
  const drafts = p.drafts || {};
  const email = drafts.email || null;
  const whatsapp = drafts.whatsapp || null;
  const task = drafts.task || null;
  return { email, whatsapp, task };
}

export function ActionInbox() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<'pending' | 'approved' | 'rejected'>('pending');
  const [priority, setPriority] = useState<'low' | 'medium' | 'high' | ''>('');

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['productivity-proposals', status, priority],
    queryFn: () => productivityApi.listProposals({ status, priority: priority || undefined, limit: 100 }),
  });
  const [timedOut, setTimedOut] = useState(false);
  useEffect(() => {
    if (isLoading) {
      const id = setTimeout(() => setTimedOut(true), 10000);
      return () => clearTimeout(id);
    }
    setTimedOut(false);
  }, [isLoading]);

  const proposals = useMemo(() => data?.data.data || [], [data]);

  const decideMutation = useMutation({
    mutationFn: ({ id, decision, reason }: { id: string; decision: 'approved' | 'rejected'; reason?: string }) =>
      productivityApi.decide(id, decision, reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['productivity-proposals'] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Action Inbox</h1>
          <p className="text-gray-500 dark:text-gray-400">Proposed actions require human approval.</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
          {(['pending', 'approved', 'rejected'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={clsx(
                'px-4 py-2 text-sm font-medium transition-colors',
                status === s
                  ? 'bg-primary-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
              )}
            >
              {s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2 text-sm text-gray-500">
          <Filter size={16} />
          <select
            value={priority}
            onChange={(e) => setPriority(e.target.value as any)}
            className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md px-2 py-2 text-sm text-gray-700 dark:text-gray-200"
          >
            <option value="">All priorities</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
        </div>
      </div>

      <div className="space-y-4">
        {isLoading ? (
          <div className="card p-8 text-center text-gray-500">Loading proposals...</div>
        ) : timedOut || error ? (
          <div className="card p-8 text-center text-red-500 space-y-3">
            <div>{timedOut ? 'Request timed out' : 'Failed to load proposals'}</div>
            <button className="btn btn-secondary" onClick={() => { setTimedOut(false); refetch(); }}>
              Retry
            </button>
          </div>
        ) : proposals.length === 0 ? (
          <div className="card p-8 text-center text-gray-500">
            {status === 'pending' ? 'No pending actions 🎉' : 'No proposals found'}
          </div>
        ) : (
          proposals.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              onApprove={(reason) => decideMutation.mutate({ id: p.id, decision: 'approved', reason })}
              onReject={(reason) => decideMutation.mutate({ id: p.id, decision: 'rejected', reason })}
              isLoading={decideMutation.isPending}
            />
          ))
        )}
      </div>
    </div>
  );
}

function ProposalCard({
  proposal,
  onApprove,
  onReject,
  isLoading,
}: {
  proposal: ProductivityProposal;
  onApprove: (reason?: string) => void;
  onReject: (reason?: string) => void;
  isLoading?: boolean;
}) {
  const [showReject, setShowReject] = useState(false);
  const [reason, setReason] = useState('');
  const { email, whatsapp, task } = pickDrafts(proposal);

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <div className="font-medium text-gray-900 dark:text-white">
              {proposal.actionType} · {proposal.targetEntity}
            </div>
            <span className={clsx('text-xs px-2 py-1 rounded-full', priorityStyles[String(proposal.priority)] || priorityStyles.low)}>
              {String(proposal.priority).toUpperCase()}
            </span>
          </div>
          <div className="mt-2 text-sm text-gray-600 dark:text-gray-300 whitespace-pre-wrap">{proposal.justification}</div>
        </div>

        {proposal.status === 'pending' && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={isLoading}
              onClick={() => onApprove()}
              className="px-3 py-2 rounded-md bg-green-600 hover:bg-green-700 text-white text-sm flex items-center gap-2 disabled:opacity-60"
            >
              <CheckCircle size={16} />
              Approve
            </button>
            <button
              type="button"
              disabled={isLoading}
              onClick={() => setShowReject((v) => !v)}
              className="px-3 py-2 rounded-md bg-red-600 hover:bg-red-700 text-white text-sm flex items-center gap-2 disabled:opacity-60"
            >
              <XCircle size={16} />
              Reject
            </button>
          </div>
        )}
      </div>

      {(email || whatsapp || task) && (
        <div className="mt-4 grid gap-3">
          {email && (
            <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
              <div className="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200">
                <Mail size={16} />
                Email Draft
              </div>
              {email.subject && <div className="mt-2 text-sm text-gray-700 dark:text-gray-200">Subject: {email.subject}</div>}
              {email.body && <div className="mt-2 text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap">{email.body}</div>}
            </div>
          )}
          {whatsapp && (
            <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
              <div className="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200">
                <MessageCircle size={16} />
                WhatsApp Draft
              </div>
              {whatsapp.message && <div className="mt-2 text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap">{whatsapp.message}</div>}
            </div>
          )}
          {task && (
            <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
              <div className="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200">
                <ClipboardList size={16} />
                Task Draft
              </div>
              {task.description && <div className="mt-2 text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap">{task.description}</div>}
            </div>
          )}
        </div>
      )}

      {showReject && proposal.status === 'pending' && (
        <div className="mt-4 p-3 rounded-lg border border-gray-200 dark:border-gray-700">
          <div className="text-sm font-medium text-gray-900 dark:text-white">Rejection reason (optional)</div>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="mt-2 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
            rows={3}
          />
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              disabled={isLoading}
              onClick={() => onReject(reason.trim() || undefined)}
              className="px-3 py-2 rounded-md bg-red-600 hover:bg-red-700 text-white text-sm disabled:opacity-60"
            >
              Confirm Reject
            </button>
            <button
              type="button"
              onClick={() => setShowReject(false)}
              className="px-3 py-2 rounded-md border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

