'use client';

import { useState } from 'react';
import { ReplayControls } from '@/components/ReplayControls';
import { EventTimeline } from '@/components/EventTimeline';

export default function ReplayPage() {
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [aggregateType, setAggregateType] = useState<string | null>(null);
  const [aggregateId, setAggregateId] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Replay & Time Travel</h1>
        <p className="text-gray-500 dark:text-gray-400">
          Start a replay job, inspect the persisted timeline, scrub by version, and compute diffs.
        </p>
      </div>

      <ReplayControls
        onJobStarted={({ jobId, tenantId, aggregateType, aggregateId }) => {
          setJobId(jobId);
          setTenantId(tenantId);
          setAggregateType(aggregateType);
          setAggregateId(aggregateId);
        }}
      />

      {tenantId && aggregateType && aggregateId && (
        <EventTimeline tenantId={tenantId} aggregateType={aggregateType} aggregateId={aggregateId} jobId={jobId} />
      )}
    </div>
  );
}

