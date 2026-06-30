'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { auditApi, governanceApi } from '@/lib/api';
import { ExplainabilityPanel } from '@/components/ExplainabilityPanel';
import { clsx } from 'clsx';
import { Search } from 'lucide-react';

export function AuditSearch() {
  const [q, setQ] = useState('');
  const [fromTs, setFromTs] = useState('');
  const [toTs, setToTs] = useState('');
  const [agentName, setAgentName] = useState('');
  const [actionType, setActionType] = useState('');
  const [status, setStatus] = useState('');
  const [riskLevel, setRiskLevel] = useState('');
  const [selectedDecisionId, setSelectedDecisionId] = useState<string | null>(null);

  const searchMutation = useMutation({
    mutationFn: () =>
      auditApi.search({
        query: q.trim(),
        fromTs: fromTs.trim() || undefined,
        toTs: toTs.trim() || undefined,
        agentName: agentName.trim() || undefined,
        actionType: actionType.trim() || undefined,
        status: status.trim() || undefined,
        riskLevel: riskLevel.trim() || undefined,
      }),
  });

  const hits = useMemo(() => searchMutation.data?.data?.hits || [], [searchMutation.data]);

  const decisionQuery = useQuery({
    queryKey: ['governance', 'decision', selectedDecisionId],
    queryFn: async () => (await governanceApi.decision(selectedDecisionId!)).data,
    enabled: Boolean(selectedDecisionId),
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="card p-6 space-y-4">
        <div>
          <div className="font-medium text-gray-900 dark:text-white">Semantic Audit Search</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            Search AI decisions by meaning with structured filters. Every access is logged.
          </div>
        </div>

        <div className="space-y-2">
          <div className="text-xs text-gray-500 dark:text-gray-400">Query</div>
          <div className="flex gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder='e.g. "show all accesses to customer PII last week"'
              className="flex-1 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
            />
            <button
              type="button"
              disabled={!q.trim() || searchMutation.isPending}
              onClick={() => searchMutation.mutate()}
              className="px-3 py-2 rounded-md bg-primary-600 hover:bg-primary-700 text-white text-sm flex items-center gap-2 disabled:opacity-60"
            >
              <Search size={16} />
              Search
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Field label="From (ISO timestamp)" value={fromTs} onChange={setFromTs} placeholder="2026-01-01T00:00:00Z" />
          <Field label="To (ISO timestamp)" value={toTs} onChange={setToTs} placeholder="2026-01-31T23:59:59Z" />
          <Field label="Agent" value={agentName} onChange={setAgentName} placeholder="automation-executor-agent" />
          <Field label="Action type" value={actionType} onChange={setActionType} placeholder="crm.automation.action.requested" />
          <Field label="Status" value={status} onChange={setStatus} placeholder="executed" />
          <Field label="Risk level" value={riskLevel} onChange={setRiskLevel} placeholder="HIGH" />
        </div>

        {searchMutation.error && <div className="text-sm text-red-600">Search failed (check permissions and Weaviate).</div>}

        <div className="space-y-2">
          <div className="text-sm font-medium text-gray-900 dark:text-white">Results</div>
          {searchMutation.isPending ? (
            <div className="text-sm text-gray-500">Searching...</div>
          ) : hits.length === 0 ? (
            <div className="text-sm text-gray-500">No hits</div>
          ) : (
            <div className="space-y-2">
              {hits.map((h: any) => (
                <button
                  key={h.decision_id}
                  type="button"
                  onClick={() => setSelectedDecisionId(String(h.decision_id))}
                  className={clsx(
                    'w-full text-left p-3 rounded-lg border transition-colors',
                    selectedDecisionId === h.decision_id
                      ? 'border-primary-500 bg-primary-50 dark:bg-primary-950'
                      : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-gray-900 dark:text-white truncate">{h.action_type}</div>
                      <div className="text-xs text-gray-500 truncate">{h.agent_name}</div>
                    </div>
                    <div className="text-xs text-gray-500">{(h.score ?? 0).toFixed(2)}</div>
                  </div>
                  <div className="mt-2 text-xs text-gray-600 dark:text-gray-300 line-clamp-3 whitespace-pre-wrap">
                    {h.snippet}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="space-y-4">
        <ExplainabilityPanel decision={decisionQuery.data} />
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div>
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
      />
    </div>
  );
}

