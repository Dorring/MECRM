'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { ExplainabilityPanel } from '@/components/ExplainabilityPanel';
import { governanceApi } from '@/lib/api';

export default function AgentRunEvidencePage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;
  const runQuery = useQuery({
    queryKey: ['agent-run-evidence', runId],
    queryFn: async () => (await governanceApi.decision(runId)).data,
    enabled: Boolean(runId),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Agent run evidence</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Tenant-scoped safe evidence. Prompts, chain-of-thought, and raw tool payloads are not displayed.
          </p>
        </div>
        <Link href="/governance" className="btn btn-secondary">Back to Governance</Link>
      </div>

      {runQuery.isLoading ? <div className="card p-6 text-gray-500">Loading agent run...</div> : null}
      {runQuery.isError ? <div className="card p-6 text-red-600">Agent run is unavailable for this tenant.</div> : null}
      {runQuery.data ? <ExplainabilityPanel decision={runQuery.data} /> : null}
    </div>
  );
}
