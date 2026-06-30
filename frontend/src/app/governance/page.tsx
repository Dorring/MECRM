'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Bot, FileText, Search, Shield } from 'lucide-react';
import { approvalsApi, auditApi, governanceApi } from '@/lib/api';
import { clsx } from 'clsx';
import { formatDistanceToNow } from 'date-fns';
import { KillSwitch } from '@/components/KillSwitch';
import { AuditSearch } from '@/components/AuditSearch';
import { ExplainabilityPanel } from '@/components/ExplainabilityPanel';

type Tab = 'kill_switch' | 'audit_search' | 'approvals' | 'decisions' | 'policies';

export default function GovernancePage() {
  const [tab, setTab] = useState<Tab>('kill_switch');
  const [selectedDecisionId, setSelectedDecisionId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const approvalsQuery = useQuery({
    queryKey: ['governance', 'approvals', 'pending'],
    queryFn: async () => (await approvalsApi.list({ status: 'pending', limit: 50 })).data,
    refetchInterval: 2000,
  });

  const decisionsQuery = useQuery({
    queryKey: ['governance', 'decisions'],
    queryFn: async () => (await governanceApi.decisions({ limit: 50 })).data,
    refetchInterval: 5000,
  });

  const decisionDetailQuery = useQuery({
    queryKey: ['governance', 'decision', selectedDecisionId],
    queryFn: async () => (await governanceApi.decision(selectedDecisionId!)).data,
    enabled: !!selectedDecisionId,
  });

  const policiesQuery = useQuery({
    queryKey: ['audit', 'policies'],
    queryFn: async () => (await auditApi.policies()).data,
    enabled: tab === 'policies',
  });

  const approvals = approvalsQuery.data?.data || [];
  const decisions = decisionsQuery.data?.data || [];

  const approvalMutation = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'approved' | 'rejected' }) =>
      approvalsApi.decide(id, decision),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['governance', 'approvals', 'pending'] });
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Governance</h1>
          <p className="text-gray-500 dark:text-gray-400">
            Kill switch, approvals, and explainability artifacts
          </p>
        </div>
      </div>

      <div className="flex rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
        <TabButton tab="kill_switch" current={tab} onClick={() => setTab('kill_switch')}>
          <Shield size={16} className="mr-2" />
          Kill Switch
        </TabButton>
        <TabButton tab="audit_search" current={tab} onClick={() => setTab('audit_search')}>
          <Search size={16} className="mr-2" />
          Audit Search
        </TabButton>
        <TabButton tab="approvals" current={tab} onClick={() => setTab('approvals')}>
          <Bot size={16} className="mr-2" />
          Approvals
        </TabButton>
        <TabButton tab="decisions" current={tab} onClick={() => setTab('decisions')}>
          <AlertTriangle size={16} className="mr-2" />
          Decisions
        </TabButton>
        <TabButton tab="policies" current={tab} onClick={() => setTab('policies')}>
          <FileText size={16} className="mr-2" />
          Policies
        </TabButton>
      </div>

      {tab === 'kill_switch' && <KillSwitch />}

      {tab === 'audit_search' && <AuditSearch />}

      {tab === 'approvals' && (
        <div className="card p-6 space-y-4">
          <div className="flex items-center justify-between">
            <div className="font-medium text-gray-900 dark:text-white">
              Pending approvals ({approvals.length})
            </div>
          </div>
          {approvals.length === 0 ? (
            <div className="text-gray-500">No pending approvals</div>
          ) : (
            <div className="space-y-3">
              {approvals.map((a) => (
                <div key={a.id} className="p-4 rounded-lg border border-gray-200 dark:border-gray-700">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="font-medium text-gray-900 dark:text-white">{a.actionType}</div>
                      <div className="text-sm text-gray-500">
                        {a.requestorType} • created {formatDistanceToNow(new Date(a.createdAt), { addSuffix: true })}
                      </div>
                    </div>
                    <div className="text-sm text-gray-500">
                      {a.expiresAt ? `expires ${formatDistanceToNow(new Date(a.expiresAt), { addSuffix: true })}` : null}
                    </div>
                  </div>
                  {a.context?.reasoning && (
                    <div className="mt-3 text-sm text-gray-700 dark:text-gray-300">{a.context.reasoning}</div>
                  )}
                  <div className="flex gap-2 mt-4">
                    <button
                      className="btn btn-secondary flex-1"
                      onClick={() => approvalMutation.mutate({ id: a.id, decision: 'rejected' })}
                      disabled={approvalMutation.isPending}
                    >
                      Reject
                    </button>
                    <button
                      className="btn btn-primary flex-1"
                      onClick={() => approvalMutation.mutate({ id: a.id, decision: 'approved' })}
                      disabled={approvalMutation.isPending}
                    >
                      Approve
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === 'decisions' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="card p-6 space-y-3">
            <div className="font-medium text-gray-900 dark:text-white">Decision History</div>
            {decisions.length === 0 ? (
              <div className="text-gray-500">No decisions recorded</div>
            ) : (
              <div className="space-y-2">
                {decisions.map((d) => (
                  <button
                    key={d.id}
                    onClick={() => setSelectedDecisionId(d.id)}
                    className={clsx(
                      'w-full text-left p-3 rounded-lg border transition-colors',
                      selectedDecisionId === d.id
                        ? 'border-primary-500 bg-primary-50 dark:bg-primary-950'
                        : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="font-medium text-gray-900 dark:text-white truncate">{d.actionType}</div>
                        <div className="text-xs text-gray-500 truncate">{d.agentId}</div>
                      </div>
                      <div className="text-xs text-gray-500">{d.status}</div>
                    </div>
                    <div className="mt-2 text-xs text-gray-500">
                      {formatDistanceToNow(new Date(d.createdAt), { addSuffix: true })}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="space-y-3">
            {decisionDetailQuery.isLoading && selectedDecisionId ? (
              <div className="card p-6 text-gray-500">Loading decision...</div>
            ) : (
              <ExplainabilityPanel decision={decisionDetailQuery.data} />
            )}
          </div>
        </div>
      )}

      {tab === 'policies' && (
        <div className="card p-6 space-y-3">
          <div className="font-medium text-gray-900 dark:text-white">Policy Visibility</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            Lists policies bundled with the deployment. Every access is logged.
          </div>
          {policiesQuery.isLoading ? (
            <div className="text-gray-500">Loading...</div>
          ) : policiesQuery.error ? (
            <div className="text-red-600 space-y-2">
              <div>Failed to load policies</div>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => policiesQuery.refetch()}
              >
                Retry
              </button>
            </div>
          ) : (
            <pre className="text-xs bg-gray-50 dark:bg-gray-900 p-4 rounded-lg overflow-auto max-h-[540px]">
{JSON.stringify(policiesQuery.data, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function TabButton({
  tab,
  current,
  onClick,
  children,
}: {
  tab: Tab;
  current: Tab;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'px-4 py-2 text-sm font-medium transition-colors flex items-center',
        current === tab
          ? 'bg-primary-600 text-white'
          : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
      )}
    >
      {children}
    </button>
  );
}
