'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { replayApi } from '@/lib/api';

function getTenantIdFromToken(): string | null {
  if (typeof window === 'undefined') return null;
  const token = localStorage.getItem('accessToken');
  if (!token) return null;
  const parts = token.split('.');
  if (parts.length < 2) return null;
  const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
  try {
    const payload = JSON.parse(atob(padded));
    return payload.tenant_id || payload.tenantId || null;
  } catch {
    return null;
  }
}

export type ReplayMode = 'offset' | 'time';

export interface ReplayControlsProps {
  onJobStarted: (args: { jobId: string; tenantId: string; aggregateType: string; aggregateId: string }) => void;
}

export function ReplayControls({ onJobStarted }: ReplayControlsProps) {
  const [aggregateType, setAggregateType] = useState<'lead' | 'ticket'>('lead');
  const [aggregateId, setAggregateId] = useState('');
  const [mode, setMode] = useState<ReplayMode>('offset');
  const [offset, setOffset] = useState('0');
  const [targetTime, setTargetTime] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);

  const tenantId = useMemo(() => getTenantIdFromToken(), []);

  const startMutation = useMutation({
    mutationFn: async () => {
      if (!tenantId) throw new Error('Missing tenant_id in access token');
      if (!aggregateId) throw new Error('aggregate_id is required');
      if (mode === 'offset') {
        return replayApi.start({
          tenant_id: tenantId,
          aggregate_type: aggregateType,
          aggregate_id: aggregateId,
          mode,
          offset: Number(offset || '0'),
        });
      }
      return replayApi.start({
        tenant_id: tenantId,
        aggregate_type: aggregateType,
        aggregate_id: aggregateId,
        mode,
        offset: Number(offset || '0'),
        target_time: new Date(targetTime).toISOString(),
      });
    },
    onSuccess: (res) => {
      const id = res.data.job_id as string;
      setJobId(id);
      if (tenantId) {
        onJobStarted({ jobId: id, tenantId, aggregateType, aggregateId });
      }
    },
  });

  const statusQuery = useQuery({
    queryKey: ['replay-status', jobId],
    queryFn: () => replayApi.status(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (q) => (q.state.data?.data?.status === 'running' ? 1000 : false),
  });

  return (
    <div className="card space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Replay Controls</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">Start a tenant-scoped replay job and poll status.</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div>
          <label className="text-xs text-gray-500">Aggregate Type</label>
          <select className="input w-full" value={aggregateType} onChange={(e) => setAggregateType(e.target.value as any)}>
            <option value="lead">Lead</option>
            <option value="ticket">Ticket</option>
          </select>
        </div>
        <div className="md:col-span-2">
          <label className="text-xs text-gray-500">Aggregate ID</label>
          <input className="input w-full" value={aggregateId} onChange={(e) => setAggregateId(e.target.value)} placeholder="UUID" />
        </div>
        <div>
          <label className="text-xs text-gray-500">Mode</label>
          <select className="input w-full" value={mode} onChange={(e) => setMode(e.target.value as ReplayMode)}>
            <option value="offset">Offset</option>
            <option value="time">Time</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="text-xs text-gray-500">Start Offset</label>
          <input className="input w-full" value={offset} onChange={(e) => setOffset(e.target.value)} />
        </div>
        <div className="md:col-span-2">
          <label className="text-xs text-gray-500">Target Time (for mode=time)</label>
          <input className="input w-full" type="datetime-local" value={targetTime} onChange={(e) => setTargetTime(e.target.value)} />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button className="btn btn-primary" onClick={() => startMutation.mutate()} disabled={startMutation.isPending}>
          {startMutation.isPending ? 'Starting…' : 'Start Replay'}
        </button>
        {jobId && (
          <div className="text-sm text-gray-600 dark:text-gray-300">
            Job: <span className="font-mono">{jobId}</span>
          </div>
        )}
      </div>

      {startMutation.isError && <div className="text-sm text-red-500">{(startMutation.error as Error).message}</div>}

      {statusQuery.data && (
        <div className="text-sm text-gray-700 dark:text-gray-300">
          Status: <span className="font-medium">{statusQuery.data.data.status}</span>{' '}
          <span className="text-gray-500">events_processed={statusQuery.data.data.events_processed}</span>
        </div>
      )}
    </div>
  );
}

