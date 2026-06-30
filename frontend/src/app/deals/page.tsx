'use client';

import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Search,
  Filter,
  DollarSign,
  TrendingUp,
  Calendar,
  User,
  ArrowRight,
  Edit,
  Trash2
} from 'lucide-react';
import { dealsApi, Deal } from '@/lib/api';
import { clsx } from 'clsx';
import { format } from 'date-fns';

const stages = [
  { id: 'prospecting', label: 'Prospecting', color: 'bg-gray-500' },
  { id: 'qualification', label: 'Qualification', color: 'bg-blue-500' },
  { id: 'proposal', label: 'Proposal', color: 'bg-yellow-500' },
  { id: 'negotiation', label: 'Negotiation', color: 'bg-orange-500' },
  { id: 'closed_won', label: 'Closed Won', color: 'bg-green-500' },
  { id: 'closed_lost', label: 'Closed Lost', color: 'bg-red-500' },
];

const stageColors: Record<string, string> = {
  prospecting: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300',
  qualification: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300',
  proposal: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-300',
  negotiation: 'bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-300',
  closed_won: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300',
  closed_lost: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300',
};

export default function DealsPage() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<'table' | 'pipeline'>('pipeline');
  const [page, setPage] = useState(1);
  const [stageFilter, setStageFilter] = useState<string>('');

  // Fetch deals
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['deals', page, stageFilter],
    queryFn: () => dealsApi.list({ page, limit: 50, stage: stageFilter || undefined }),
  });
  const [timedOut, setTimedOut] = useState(false);

  useEffect(() => {
    if (isLoading) {
      const id = setTimeout(() => setTimedOut(true), 10000);
      return () => clearTimeout(id);
    }
    setTimedOut(false);
  }, [isLoading]);

  // Update stage mutation
  const updateStageMutation = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: string }) =>
      dealsApi.updateStage(id, stage),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['deals'] });
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => dealsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['deals'] });
    },
  });

  const deals = data?.data.data || [];

  // Group deals by stage for pipeline view
  const dealsByStage = stages.reduce((acc, stage) => {
    acc[stage.id] = deals.filter((deal) => deal.stage === stage.id);
    return acc;
  }, {} as Record<string, Deal[]>);

  // Calculate totals
  const totalValue = deals
    .filter((d) => !d.stage.startsWith('closed'))
    .reduce((sum, d) => sum + (d.amount || 0), 0);

  const formatCurrency = (amount: number) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Deals
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Pipeline value: <span className="font-semibold text-primary-600">{formatCurrency(totalValue)}</span>
          </p>
        </div>
        <div className="flex items-center gap-4">
          {/* View toggle */}
          <div className="flex rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
            <button
              onClick={() => setView('pipeline')}
              className={clsx(
                'px-4 py-2 text-sm font-medium transition-colors',
                view === 'pipeline'
                  ? 'bg-primary-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300'
              )}
            >
              Pipeline
            </button>
            <button
              onClick={() => setView('table')}
              className={clsx(
                'px-4 py-2 text-sm font-medium transition-colors',
                view === 'table'
                  ? 'bg-primary-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300'
              )}
            >
              Table
            </button>
          </div>
          {/* TODO(Phase 5): "Add Deal" create form/modal does not exist yet.
              Hidden to avoid shipping a dead button. Wire to a DealFormModal
              (mirroring LeadFormModal) once the create-deal field contract is
              defined with the gateway. */}
        </div>
      </div>

      {isLoading || isFetching ? (
        <div className="p-8 text-center text-gray-500">Loading deals...</div>
      ) : timedOut || error ? (
        <div className="p-8 text-center text-red-500 space-y-3">
          <div>{timedOut ? 'Request timed out' : 'Failed to load deals'}</div>
          <button className="btn btn-secondary" onClick={() => { setTimedOut(false); refetch(); }}>
            Retry
          </button>
        </div>
      ) : view === 'pipeline' ? (
        /* Pipeline view */
        <div className="flex gap-4 overflow-x-auto pb-4">
          {stages.map((stage) => {
            const stageDeals = dealsByStage[stage.id] || [];
            const stageValue = stageDeals.reduce((sum, d) => sum + (d.amount || 0), 0);

            return (
              <div
                key={stage.id}
                className="flex-shrink-0 w-72 bg-gray-50 dark:bg-gray-800 rounded-lg"
              >
                {/* Stage header */}
                <div className="p-4 border-b border-gray-200 dark:border-gray-700">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <div className={clsx('w-3 h-3 rounded-full', stage.color)} />
                      <h3 className="font-medium text-gray-900 dark:text-white">
                        {stage.label}
                      </h3>
                    </div>
                    <span className="text-sm text-gray-500">{stageDeals.length}</span>
                  </div>
                  <p className="text-sm text-gray-500 mt-1">
                    {formatCurrency(stageValue)}
                  </p>
                </div>

                {/* Deals */}
                <div className="p-2 space-y-2 max-h-[600px] overflow-y-auto">
                  {stageDeals.map((deal) => (
                    <DealCard
                      key={deal.id}
                      deal={deal}
                      onMoveNext={() => {
                        const currentIdx = stages.findIndex((s) => s.id === deal.stage);
                        if (currentIdx < stages.length - 2) {
                          updateStageMutation.mutate({
                            id: deal.id,
                            stage: stages[currentIdx + 1].id,
                          });
                        }
                      }}
                      onDelete={() => deleteMutation.mutate(deal.id)}
                    />
                  ))}
                  {stageDeals.length === 0 && (
                    <div className="p-4 text-center text-sm text-gray-400">
                      No deals
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        /* Table view */
        <div className="card overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-50 dark:bg-gray-800">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Deal
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Amount
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Stage
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Probability
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Expected Close
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Assigned To
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {deals.map((deal) => (
                  <tr
                    key={deal.id}
                    className="hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                  >
                    <td className="px-6 py-4">
                      <div className="font-medium text-gray-900 dark:text-white">
                        {deal.name}
                      </div>
                      {deal.customer && (
                        <div className="text-sm text-gray-500">{deal.customer.name}</div>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center text-gray-900 dark:text-white font-medium">
                        <DollarSign size={16} className="mr-1 text-green-500" />
                        {deal.amount ? formatCurrency(deal.amount) : '—'}
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      <span className={clsx('badge', stageColors[deal.stage])}>
                        {stages.find((s) => s.id === deal.stage)?.label || deal.stage}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center">
                        <div className="w-16 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-primary-500"
                            style={{ width: `${deal.probability}%` }}
                          />
                        </div>
                        <span className="ml-2 text-sm text-gray-500">{deal.probability}%</span>
                      </div>
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-500 dark:text-gray-400">
                      {deal.expectedCloseDate
                        ? format(new Date(deal.expectedCloseDate), 'MMM d, yyyy')
                        : '—'}
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-900 dark:text-white">
                      {deal.assignedUser?.name || <span className="text-gray-400">Unassigned</span>}
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">
                          <Edit size={16} className="text-gray-500" />
                        </button>
                        <button
                          onClick={() => deleteMutation.mutate(deal.id)}
                          className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                        >
                          <Trash2 size={16} className="text-red-500" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// Deal card component for pipeline view
function DealCard({
  deal,
  onMoveNext,
  onDelete,
}: {
  deal: Deal;
  onMoveNext: () => void;
  onDelete: () => void;
}) {
  const formatCurrency = (amount: number) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);

  return (
    <div className="bg-white dark:bg-gray-900 rounded-lg p-3 shadow-sm border border-gray-200 dark:border-gray-700">
      <div className="flex items-start justify-between">
        <h4 className="font-medium text-gray-900 dark:text-white text-sm">
          {deal.name}
        </h4>
        {!deal.stage.startsWith('closed') && (
          <button
            onClick={onMoveNext}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
            title="Move to next stage"
          >
            <ArrowRight size={14} className="text-gray-400" />
          </button>
        )}
      </div>

      <div className="mt-2 space-y-1">
        {deal.amount && (
          <div className="flex items-center text-sm">
            <DollarSign size={14} className="mr-1 text-green-500" />
            <span className="font-medium">{formatCurrency(deal.amount)}</span>
          </div>
        )}

        {deal.expectedCloseDate && (
          <div className="flex items-center text-xs text-gray-500">
            <Calendar size={12} className="mr-1" />
            {format(new Date(deal.expectedCloseDate), 'MMM d')}
          </div>
        )}

        {deal.customer && (
          <div className="flex items-center text-xs text-gray-500">
            <User size={12} className="mr-1" />
            {deal.customer.name}
          </div>
        )}
      </div>

      <div className="mt-3 flex items-center justify-between">
        <div className="flex items-center">
          <TrendingUp size={12} className="mr-1 text-primary-500" />
          <span className="text-xs text-gray-500">{deal.probability}%</span>
        </div>
        <button
          onClick={onDelete}
          className="p-1 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
        >
          <Trash2 size={12} className="text-red-500" />
        </button>
      </div>
    </div>
  );
}
