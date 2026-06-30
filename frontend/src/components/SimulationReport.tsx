'use client';

import { clsx } from 'clsx';

export function SimulationReport({ result }: { result: any }) {
  if (!result) return null;
  const would = result.would_have_triggered ?? 0;
  const impact = result.estimated_impact || {};
  const samples = Array.isArray(result.sample_actions) ? result.sample_actions : [];
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium text-gray-900 dark:text-white">Simulation Report</div>
        <div className={clsx('text-sm', would > 0 ? 'text-orange-600' : 'text-gray-500')}>
          Would have triggered: <span className="font-semibold">{would}</span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <Stat label="Tasks created" value={impact.tasks_created ?? 0} />
        <Stat label="Notifications" value={impact.notifications ?? 0} />
        <Stat label="Followups" value={impact.followups ?? 0} />
      </div>

      {warnings.length > 0 && (
        <div className="p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 text-amber-900 dark:text-amber-200 text-sm">
          <div className="font-medium">Warnings</div>
          <div className="mt-1 whitespace-pre-wrap">{warnings.join('\n')}</div>
        </div>
      )}

      {samples.length > 0 && (
        <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
          <div className="text-sm font-medium text-gray-800 dark:text-gray-200">Sample actions</div>
          <pre className="mt-2 text-xs overflow-auto text-gray-700 dark:text-gray-200">
            {JSON.stringify(samples.slice(0, 10), null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="p-3 rounded-lg border border-gray-200 dark:border-gray-700">
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
      <div className="mt-1 text-lg font-semibold text-gray-900 dark:text-white">{value}</div>
    </div>
  );
}

