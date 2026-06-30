'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { customersApi } from '@/lib/api';
import { clsx } from 'clsx';
import { CalendarClock, TrendingUp, Ticket, User, BadgeDollarSign, CheckCircle2, ShieldCheck } from 'lucide-react';
import { RiskBadges } from './RiskBadges';

type TimelineRow = {
  id: string;
  eventType: string;
  eventPayload: any;
  ts: string;
};

const iconFor = (eventType: string) => {
  if (eventType.startsWith('ticket.')) return Ticket;
  if (eventType.startsWith('deal.')) return TrendingUp;
  if (eventType.startsWith('customer.')) return User;
  if (eventType.startsWith('approval.')) return ShieldCheck;
  if (eventType.includes('payment')) return BadgeDollarSign;
  return CalendarClock;
};

export function CustomerTimeline({ customerId }: { customerId: string }) {
  const profileQuery = useQuery({
    queryKey: ['customerProfile', customerId],
    queryFn: () => customersApi.profile(customerId),
    enabled: Boolean(customerId),
  });

  const timelineQuery = useQuery({
    queryKey: ['customerTimeline', customerId],
    queryFn: () => customersApi.timeline(customerId, { limit: 80 }),
    enabled: Boolean(customerId),
  });

  const stage = profileQuery.data?.data?.stage || 'awareness';
  const confidence = typeof profileQuery.data?.data?.confidence === 'number' ? profileQuery.data?.data?.confidence : 0;
  const predictions = profileQuery.data?.data?.predictions || [];

  const entries = useMemo(() => {
    const rows = (timelineQuery.data?.data?.data || []) as any[];
    return rows.map((r) => ({
      id: r.id,
      eventType: r.eventType,
      eventPayload: r.eventPayload,
      ts: r.ts,
    })) as TimelineRow[];
  }, [timelineQuery.data]);

  return (
    <div className="card space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm text-gray-500 dark:text-gray-400">Stage</div>
          <div className="mt-1 flex items-center gap-2">
            <span className={clsx('badge', 'badge-info')}>{stage}</span>
            <span className="text-xs text-gray-500 dark:text-gray-400">{(confidence * 100).toFixed(0)}%</span>
          </div>
        </div>
        <div className="flex items-center justify-end">
          <RiskBadges predictions={predictions} />
        </div>
      </div>

      {timelineQuery.isLoading ? (
        <div className="text-sm text-gray-500">Loading timeline…</div>
      ) : entries.length === 0 ? (
        <div className="text-sm text-gray-500">No timeline events yet.</div>
      ) : (
        <ol className="relative border-l border-gray-200 dark:border-gray-800 space-y-6 pl-6">
          {entries.map((e) => {
            const Icon = iconFor(e.eventType);
            const title = e.eventType.replace('_', ' ');
            const payloadSummary = e.eventPayload && typeof e.eventPayload === 'object' ? Object.entries(e.eventPayload).slice(0, 4) : [];
            return (
              <li key={e.id} className="relative">
                <span className="absolute -left-[13px] top-0 flex items-center justify-center w-6 h-6 rounded-full bg-gray-100 dark:bg-gray-800">
                  <Icon size={14} className="text-gray-600 dark:text-gray-300" />
                </span>
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <div className="text-sm font-medium text-gray-900 dark:text-white flex items-center gap-2">
                      <span>{title}</span>
                      {e.eventType === 'ticket.resolved' && <CheckCircle2 size={14} className="text-green-600" />}
                    </div>
                    {payloadSummary.length > 0 && (
                      <div className="mt-1 text-xs text-gray-600 dark:text-gray-300">
                        {payloadSummary.map(([k, v]) => (
                          <span key={k} className="mr-3">
                            {k}={String(v)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="text-xs text-gray-500">{new Date(e.ts).toLocaleString()}</div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

