'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { clsx } from 'clsx';
import { automationsApi } from '@/lib/api';
import { SimulationReport } from '@/components/SimulationReport';
import { CheckCircle, Clock, PauseCircle, PlayCircle, RefreshCw, Shield, Trash2 } from 'lucide-react';

const statusPill: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200',
  simulating: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200',
  active: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-200',
  paused: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-200',
  disabled: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-200',
};

export function AutomationStudio() {
  const queryClient = useQueryClient();
  const [nlRuleText, setNlRuleText] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const policiesQuery = useQuery({
    queryKey: ['automations'],
    queryFn: () => automationsApi.list({ limit: 50 }),
  });

  const policies = useMemo(() => policiesQuery.data?.data.data || [], [policiesQuery.data]);

  const selected = useMemo(() => {
    if (!selectedId) return null;
    return policies.find((p: any) => p.id === selectedId) || null;
  }, [policies, selectedId]);

  const simulationsQuery = useQuery({
    queryKey: ['automation-simulations', selectedId],
    queryFn: () => automationsApi.simulations(selectedId!, { limit: 5 }),
    enabled: Boolean(selectedId),
    refetchInterval: 2000,
  });

  const lastSimulation = useMemo(() => {
    const sims = simulationsQuery.data?.data.data || [];
    return sims.length ? sims[0] : null;
  }, [simulationsQuery.data]);

  const parseMutation = useMutation({
    mutationFn: (text: string) => automationsApi.parse(text),
  });

  const createMutation = useMutation({
    mutationFn: (text: string) => automationsApi.create(text),
    onSuccess: (resp) => {
      queryClient.invalidateQueries({ queryKey: ['automations'] });
      const policyId = resp.data.policy?.id;
      if (policyId) setSelectedId(policyId);
    },
  });

  const simulateMutation = useMutation({
    mutationFn: (id: string) => automationsApi.simulate(id, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['automations'] });
      queryClient.invalidateQueries({ queryKey: ['automation-simulations'] });
    },
  });

  const requestActivationMutation = useMutation({
    mutationFn: (id: string) => automationsApi.requestActivation(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['approvals'] }),
  });

  const pauseMutation = useMutation({
    mutationFn: (id: string) => automationsApi.pause(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['automations'] }),
  });

  const resumeMutation = useMutation({
    mutationFn: (id: string) => automationsApi.resume(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['automations'] }),
  });

  const deactivateMutation = useMutation({
    mutationFn: (id: string) => automationsApi.deactivate(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['automations'] }),
  });

  const parsed = parseMutation.data?.data;
  const parsedWorkflow = parsed?.workflow;
  const parsedCompiled = parsed?.compiled;
  const parsedWarnings = parsed?.warnings || [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Automation Studio</h1>
          <p className="text-gray-500 dark:text-gray-400">Define workflows in English, validate via simulation, then activate safely.</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-4">
          <div className="card p-4 space-y-3">
            <div className="text-sm font-medium text-gray-900 dark:text-white">Natural language rule</div>
            <textarea
              value={nlRuleText}
              onChange={(e) => setNlRuleText(e.target.value)}
              placeholder='e.g. "When invoice overdue by 7 days, notify finance and assign call task."'
              rows={4}
              className="w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm text-gray-800 dark:text-gray-200"
            />

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={!nlRuleText.trim() || parseMutation.isPending}
                onClick={() => parseMutation.mutate(nlRuleText.trim())}
                className="px-3 py-2 rounded-md border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 disabled:opacity-60"
              >
                Preview
              </button>
              <button
                type="button"
                disabled={!nlRuleText.trim() || createMutation.isPending}
                onClick={() => createMutation.mutate(nlRuleText.trim())}
                className="px-3 py-2 rounded-md bg-primary-600 hover:bg-primary-700 text-white text-sm disabled:opacity-60"
              >
                Create Policy
              </button>
            </div>
          </div>

          {(parsedWorkflow || parsedCompiled) && (
            <div className="card p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="text-sm font-medium text-gray-900 dark:text-white">Parsed workflow</div>
                {parsedWarnings.length > 0 && (
                  <span className="text-xs px-2 py-1 rounded-full bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-200">
                    {parsedWarnings.length} warnings
                  </span>
                )}
              </div>
              <pre className="text-xs overflow-auto p-3 rounded-lg bg-gray-50 dark:bg-gray-800 text-gray-800 dark:text-gray-200">
                {JSON.stringify({ workflow: parsedWorkflow, compiled: parsedCompiled, warnings: parsedWarnings }, null, 2)}
              </pre>
            </div>
          )}

          {selected && (
            <div className="space-y-4">
              <div className="card p-4 space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <div className="font-medium text-gray-900 dark:text-white truncate">Selected policy</div>
                      <span className={clsx('text-xs px-2 py-1 rounded-full', statusPill[selected.status] || statusPill.draft)}>
                        {String(selected.status)}
                      </span>
                    </div>
                    <div className="mt-1 text-sm text-gray-600 dark:text-gray-300 truncate">{selected.nlRuleText}</div>
                  </div>
                </div>

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={simulateMutation.isPending}
                    onClick={() => simulateMutation.mutate(selected.id)}
                    className="px-3 py-2 rounded-md bg-amber-600 hover:bg-amber-700 text-white text-sm flex items-center gap-2 disabled:opacity-60"
                  >
                    <RefreshCw size={16} />
                    Run Simulation
                  </button>
                  <button
                    type="button"
                    disabled={requestActivationMutation.isPending || !selected.lastSimulationId}
                    onClick={() => requestActivationMutation.mutate(selected.id)}
                    className="px-3 py-2 rounded-md bg-green-600 hover:bg-green-700 text-white text-sm flex items-center gap-2 disabled:opacity-60"
                  >
                    <Shield size={16} />
                    Request Activation
                  </button>

                  {selected.status === 'active' && (
                    <button
                      type="button"
                      disabled={pauseMutation.isPending}
                      onClick={() => pauseMutation.mutate(selected.id)}
                      className="px-3 py-2 rounded-md border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 flex items-center gap-2 disabled:opacity-60"
                    >
                      <PauseCircle size={16} />
                      Pause
                    </button>
                  )}

                  {selected.status === 'paused' && (
                    <button
                      type="button"
                      disabled={resumeMutation.isPending}
                      onClick={() => resumeMutation.mutate(selected.id)}
                      className="px-3 py-2 rounded-md border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 flex items-center gap-2 disabled:opacity-60"
                    >
                      <PlayCircle size={16} />
                      Resume
                    </button>
                  )}

                  <button
                    type="button"
                    disabled={deactivateMutation.isPending}
                    onClick={() => deactivateMutation.mutate(selected.id)}
                    className="px-3 py-2 rounded-md border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 flex items-center gap-2 disabled:opacity-60"
                  >
                    <Trash2 size={16} />
                    Deactivate
                  </button>
                </div>

                {!selected.lastSimulationId && (
                  <div className="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-2">
                    <Clock size={14} />
                    Activation requires a completed simulation.
                  </div>
                )}
              </div>

              {lastSimulation?.result && <SimulationReport result={lastSimulation.result} />}
            </div>
          )}
        </div>

        <div className="space-y-4">
          <div className="card p-4">
            <div className="text-sm font-medium text-gray-900 dark:text-white">Policies</div>
            <div className="mt-3 space-y-2">
              {policiesQuery.isLoading ? (
                <div className="text-sm text-gray-500">Loading...</div>
              ) : policiesQuery.error ? (
                <div className="text-sm text-red-500 space-y-2">
                  <div>Failed to load</div>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={() => policiesQuery.refetch()}
                  >
                    Retry
                  </button>
                </div>
              ) : policies.length === 0 ? (
                <div className="text-sm text-gray-500">No policies</div>
              ) : (
                policies.map((p: any) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => setSelectedId(p.id)}
                    className={clsx(
                      'w-full text-left p-3 rounded-lg border transition-colors',
                      selectedId === p.id
                        ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/20'
                        : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800'
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-sm font-medium text-gray-900 dark:text-white truncate">{p.triggerType}</div>
                      <span className={clsx('text-xs px-2 py-1 rounded-full', statusPill[p.status] || statusPill.draft)}>
                        {String(p.status)}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-gray-500 dark:text-gray-400 truncate">{p.nlRuleText}</div>
                    {p.lastSimulationId && (
                      <div className="mt-2 text-xs text-green-700 dark:text-green-300 flex items-center gap-1">
                        <CheckCircle size={12} />
                        Simulated
                      </div>
                    )}
                  </button>
                ))
              )}
            </div>
          </div>

          {parseMutation.error && <ErrorCard title="Preview failed" />}
          {createMutation.error && <ErrorCard title="Create failed" />}
          {simulateMutation.error && <ErrorCard title="Simulation request failed" />}
          {requestActivationMutation.error && <ErrorCard title="Activation request failed" />}
        </div>
      </div>
    </div>
  );
}

function ErrorCard({ title }: { title: string }) {
  return (
    <div className="card p-4 border border-red-200 dark:border-red-900">
      <div className="text-sm font-medium text-red-700 dark:text-red-300">{title}</div>
      <div className="mt-1 text-xs text-red-600 dark:text-red-200">Check your permissions and try again.</div>
    </div>
  );
}

