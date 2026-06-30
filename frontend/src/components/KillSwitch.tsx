'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { clsx } from 'clsx';
import { governanceApi } from '@/lib/api';
import { PauseCircle, PlayCircle, XCircle } from 'lucide-react';

export function KillSwitch() {
  const queryClient = useQueryClient();
  const [tenantId, setTenantId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [reason, setReason] = useState('Updated via Governance UI');

  const statusQuery = useQuery({
    queryKey: ['governance', 'killswitch'],
    queryFn: async () => (await governanceApi.killSwitchStatus()).data,
    refetchInterval: 2000,
  });

  const activeTenantPause = useMemo(() => {
    const status = statusQuery.data;
    if (!status) return null;
    const entries = Object.entries(status.tenants || {});
    const paused = entries.find(([, s]: any) => s?.state === 'paused' || s?.state === 'killed');
    return paused ? { tenantId: paused[0], ...(paused[1] as any) } : null;
  }, [statusQuery.data]);

  const pauseMutation = useMutation({
    mutationFn: () => governanceApi.pauseTenantAgents(tenantId.trim() || undefined, reason.trim() || undefined),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['governance', 'killswitch'] }),
  });

  const resumeMutation = useMutation({
    mutationFn: () => governanceApi.resumeTenantAgents(tenantId.trim() || undefined, reason.trim() || undefined),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['governance', 'killswitch'] }),
  });

  const globalStopMutation = useMutation({
    mutationFn: () => governanceApi.emergencyStop(agentId.trim() || undefined, reason.trim() || undefined),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['governance', 'killswitch'] }),
  });

  return (
    <div className="card p-6 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-medium text-gray-900 dark:text-white">Kill Switch</div>
          <div className="text-sm text-gray-500 dark:text-gray-400">Global, tenant, or agent-level stop controls.</div>
        </div>
        <button
          className="btn btn-secondary"
          onClick={() => statusQuery.refetch()}
          disabled={statusQuery.isFetching}
        >
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatusCard title="Global" value={statusQuery.data?.global?.state || 'running'} />
        <StatusCard title="Tenant Pause" value={activeTenantPause?.state || 'none'} />
        <StatusCard title="Tenant Count" value={String(Object.keys(statusQuery.data?.tenants || {}).length)} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <div className="text-xs text-gray-500 dark:text-gray-400">Tenant (optional)</div>
          <input
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
            placeholder="tenant UUID (blank = current tenant)"
            className="mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
          />
        </div>
        <div>
          <div className="text-xs text-gray-500 dark:text-gray-400">Agent (optional)</div>
          <input
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            placeholder="agent id (blank = global)"
            className="mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
          />
        </div>
        <div>
          <div className="text-xs text-gray-500 dark:text-gray-400">Reason</div>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="reason"
            className="mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          className="btn btn-warning"
          onClick={() => pauseMutation.mutate()}
          disabled={pauseMutation.isPending}
        >
          <PauseCircle size={16} className="mr-2" />
          Pause Tenant
        </button>
        <button
          className="btn btn-primary"
          onClick={() => resumeMutation.mutate()}
          disabled={resumeMutation.isPending}
        >
          <PlayCircle size={16} className="mr-2" />
          Resume Tenant
        </button>
        <button
          className={clsx('btn btn-danger', !agentId.trim() ? 'opacity-90' : '')}
          onClick={() => globalStopMutation.mutate()}
          disabled={globalStopMutation.isPending}
        >
          <XCircle size={16} className="mr-2" />
          Emergency Stop {agentId.trim() ? '(Agent)' : '(Global)'}
        </button>
      </div>
    </div>
  );
}

function StatusCard({ title, value }: { title: string; value: string }) {
  const color =
    value === 'running'
      ? 'text-green-700 dark:text-green-300'
      : value === 'paused'
        ? 'text-amber-700 dark:text-amber-300'
        : 'text-red-700 dark:text-red-300';
  return (
    <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-700">
      <div className="text-xs text-gray-500 dark:text-gray-400">{title}</div>
      <div className={clsx('mt-1 text-lg font-semibold', color)}>{value}</div>
    </div>
  );
}

