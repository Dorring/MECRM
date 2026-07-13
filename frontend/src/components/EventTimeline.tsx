'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { replayApi } from '@/lib/api';
import { clsx } from 'clsx';

type TimelineEvent = {
  event_id: string;
  ts: string;
  event_type: string;
  version: number;
  payload_summary: Record<string, any>;
};

export interface EventTimelineProps {
  tenantId: string;
  aggregateType: string;
  aggregateId: string;
  jobId: string | null;
}

export function EventTimeline({ tenantId, aggregateType, aggregateId, jobId }: EventTimelineProps) {
  const [filter, setFilter] = useState<string>('');
  const [scrubVersion, setScrubVersion] = useState<number | null>(null);
  const [fromVersion, setFromVersion] = useState<number | null>(null);
  const [toVersion, setToVersion] = useState<number | null>(null);

  const timelineQuery = useQuery<TimelineEvent[]>({
    queryKey: ['timeline', tenantId, aggregateType, aggregateId],
    queryFn: async () => {
      const res = await replayApi.timeline(aggregateType, aggregateId, tenantId);
      return res.data as TimelineEvent[];
    },
    enabled: Boolean(tenantId && aggregateType && aggregateId),
    refetchInterval: 2000,
  });

  const events = useMemo(() => timelineQuery.data || [], [timelineQuery.data]);
  const eventTypes = useMemo<string[]>(() => Array.from(new Set<string>(events.map((e) => e.event_type))).sort(), [events]);
  const filtered = useMemo(
    () => (filter ? events.filter((e) => e.event_type === filter) : events),
    [events, filter]
  );

  const minVersion = filtered.length ? filtered[0].version : 0;
  const maxVersion = filtered.length ? filtered[filtered.length - 1].version : 0;
  const effectiveScrub = scrubVersion ?? maxVersion;

  const diffQuery = useQuery({
    queryKey: ['diff', jobId, fromVersion, toVersion],
    queryFn: async () => replayApi.diff(jobId!, fromVersion!, toVersion!),
    enabled: Boolean(jobId && fromVersion !== null && toVersion !== null && toVersion >= fromVersion),
  });

  return (
    <div className="card space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Event Timeline</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Scrub by version and view diffs between two points (requires a replay job id).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select className="input" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">All event types</option>
            {eventTypes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>

      {!events.length && <div className="text-sm text-gray-500">No events found yet for this aggregate.</div>}

      {events.length > 0 && (
        <div className="space-y-3">
          <div>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>Version scrubber</span>
              <span>
                {minVersion} → {maxVersion} (selected: {effectiveScrub})
              </span>
            </div>
            <input
              type="range"
              min={minVersion}
              max={maxVersion}
              value={effectiveScrub}
              onChange={(e) => setScrubVersion(Number(e.target.value))}
              className="w-full"
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label className="text-xs text-gray-500">From version</label>
              <select className="input w-full" value={fromVersion ?? ''} onChange={(e) => setFromVersion(Number(e.target.value))}>
                <option value="" disabled>
                  Select…
                </option>
                {events.map((e) => (
                  <option key={e.version} value={e.version}>
                    v{e.version} · {new Date(e.ts).toLocaleString()}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-500">To version</label>
              <select className="input w-full" value={toVersion ?? ''} onChange={(e) => setToVersion(Number(e.target.value))}>
                <option value="" disabled>
                  Select…
                </option>
                {events.map((e) => (
                  <option key={e.version} value={e.version}>
                    v{e.version} · {new Date(e.ts).toLocaleString()}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <div className="text-xs text-gray-500">
                {jobId ? (
                  <div>
                    Using job <span className="font-mono">{jobId}</span>
                  </div>
                ) : (
                  <div>Start a replay job to enable diffs.</div>
                )}
              </div>
            </div>
          </div>

          {diffQuery.isFetching && <div className="text-sm text-gray-500">Computing diff…</div>}
          {diffQuery.data && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div className="md:col-span-1">
                <div className="text-sm font-medium text-gray-900 dark:text-white">Changed keys</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {diffQuery.data.data.changed_keys.map((k: string) => (
                    <span key={k} className="badge badge-info">
                      {k}
                    </span>
                  ))}
                </div>
              </div>
              <div className="md:col-span-2">
                <div className="text-sm font-medium text-gray-900 dark:text-white">Before / After</div>
                <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-3">
                  <pre className="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded overflow-auto">
                    {JSON.stringify(diffQuery.data.data.before, null, 2)}
                  </pre>
                  <pre className="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded overflow-auto">
                    {JSON.stringify(diffQuery.data.data.after, null, 2)}
                  </pre>
                </div>
              </div>
            </div>
          )}

          <div className="max-h-96 overflow-auto border border-gray-200 dark:border-gray-800 rounded">
            <div className="divide-y divide-gray-200 dark:divide-gray-800">
              {filtered.map((e) => {
                const isSelected = e.version === effectiveScrub;
                return (
                  <div
                    key={e.event_id}
                    className={clsx('p-3 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800', isSelected && 'bg-primary-50 dark:bg-primary-900/20')}
                    onClick={() => setScrubVersion(e.version)}
                  >
                    <div className="flex items-center justify-between">
                      <div className="text-sm font-medium text-gray-900 dark:text-white">
                        v{e.version} · {e.event_type}
                      </div>
                      <div className="text-xs text-gray-500">{new Date(e.ts).toLocaleString()}</div>
                    </div>
                    {Object.keys(e.payload_summary || {}).length > 0 && (
                      <div className="mt-2 text-xs text-gray-600 dark:text-gray-300">
                        {Object.entries(e.payload_summary).map(([k, v]) => (
                          <span key={k} className="mr-3">
                            {k}={String(v)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

