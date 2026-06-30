'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { 
  Shield,
  Clock,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Bot,
  User,
  MessageSquare,
  DollarSign,
  Filter
} from 'lucide-react';
import { approvalsApi, Approval } from '@/lib/api';
import { clsx } from 'clsx';
import { format, formatDistanceToNow, isPast } from 'date-fns';

const statusConfig: Record<string, { color: string; label: string; icon: any }> = {
  pending: { color: 'text-yellow-500', label: 'Pending', icon: Clock },
  approved: { color: 'text-green-500', label: 'Approved', icon: CheckCircle },
  rejected: { color: 'text-red-500', label: 'Rejected', icon: XCircle },
  expired: { color: 'text-gray-500', label: 'Expired', icon: AlertTriangle },
};

const actionTypeLabels: Record<string, string> = {
  'deals:close': 'Close Deal',
  'leads:qualify': 'Qualify Lead',
  'leads:delete': 'Delete Lead',
  'customers:delete': 'Delete Customer',
  'tickets:escalate': 'Escalate Ticket',
  'deals:discount_apply': 'Apply Discount',
};

export default function ApprovalsPage() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>('pending');

  // Fetch approvals
  const { data, isLoading, error } = useQuery({
    queryKey: ['approvals', statusFilter],
    queryFn: () => approvalsApi.list({ status: statusFilter || undefined, limit: 50 }),
  });

  // Decide mutation
  const decideMutation = useMutation({
    mutationFn: ({
      id,
      decision,
      reason,
    }: {
      id: string;
      decision: 'approved' | 'rejected';
      reason?: string;
    }) => approvalsApi.decide(id, decision, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['approvals'] });
    },
  });

  const approvals = data?.data.data || [];

  // Stats
  const pendingCount = approvals.filter((a) => a.status === 'pending').length;
  const urgentCount = approvals.filter(
    (a) => a.status === 'pending' && a.expiresAt && isPast(new Date(a.expiresAt))
  ).length;

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Approvals
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            <span className="font-medium text-primary-600">{pendingCount}</span> pending approvals
            {urgentCount > 0 && (
              <span className="ml-3 text-red-500">
                <AlertTriangle size={14} className="inline mr-1" />
                {urgentCount} expired
              </span>
            )}
          </p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-4">
        <div className="flex rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
          {['pending', 'approved', 'rejected', ''].map((status) => (
            <button
              key={status}
              onClick={() => setStatusFilter(status)}
              className={clsx(
                'px-4 py-2 text-sm font-medium transition-colors',
                statusFilter === status
                  ? 'bg-primary-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
              )}
            >
              {status ? statusConfig[status]?.label : 'All'}
            </button>
          ))}
        </div>
      </div>

      {/* Approvals list */}
      <div className="space-y-4">
        {isLoading ? (
          <div className="card p-8 text-center text-gray-500">Loading approvals...</div>
        ) : error ? (
          <div className="card p-8 text-center text-red-500">Failed to load approvals</div>
        ) : approvals.length === 0 ? (
          <div className="card p-8 text-center text-gray-500">
            {statusFilter === 'pending' ? 'No pending approvals 🎉' : 'No approvals found'}
          </div>
        ) : (
          approvals.map((approval) => (
            <ApprovalCard
              key={approval.id}
              approval={approval}
              onApprove={(reason) =>
                decideMutation.mutate({ id: approval.id, decision: 'approved', reason })
              }
              onReject={(reason) =>
                decideMutation.mutate({ id: approval.id, decision: 'rejected', reason })
              }
              isLoading={decideMutation.isPending}
            />
          ))
        )}
      </div>
    </div>
  );
}

function ApprovalCard({
  approval,
  onApprove,
  onReject,
  isLoading,
}: {
  approval: Approval;
  onApprove: (reason?: string) => void;
  onReject: (reason?: string) => void;
  isLoading?: boolean;
}) {
  const [showRejectReason, setShowRejectReason] = useState(false);
  const [rejectReason, setRejectReason] = useState('');

  const status = statusConfig[approval.status] || statusConfig.pending;
  const StatusIcon = status.icon;
  const isExpiring =
    approval.status === 'pending' &&
    approval.expiresAt &&
    new Date(approval.expiresAt).getTime() - Date.now() < 3600000; // < 1 hour

  return (
    <div className={clsx(
      'card',
      approval.status === 'pending' && isExpiring && 'border-orange-300 dark:border-orange-800'
    )}>
      <div className="flex items-start gap-4">
        {/* Icon */}
        <div className={clsx(
          'p-3 rounded-lg',
          approval.requestorType === 'agent'
            ? 'bg-primary-100 dark:bg-primary-900'
            : 'bg-gray-100 dark:bg-gray-800'
        )}>
          {approval.requestorType === 'agent' ? (
            <Bot size={24} className="text-primary-600" />
          ) : (
            <User size={24} className="text-gray-600" />
          )}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="font-medium text-gray-900 dark:text-white">
                {actionTypeLabels[approval.actionType] || approval.actionType}
              </h3>
              <div className="flex items-center gap-2 mt-1 text-sm text-gray-500">
                <span className={clsx('flex items-center', status.color)}>
                  <StatusIcon size={14} className="mr-1" />
                  {status.label}
                </span>
                <span>•</span>
                <span>Requested by {approval.requestorType}</span>
              </div>
            </div>

            {/* Expiry */}
            {approval.status === 'pending' && approval.expiresAt && (
              <div className={clsx(
                'text-sm',
                isExpiring ? 'text-orange-500' : 'text-gray-500'
              )}>
                <Clock size={14} className="inline mr-1" />
                Expires {formatDistanceToNow(new Date(approval.expiresAt), { addSuffix: true })}
              </div>
            )}
          </div>

          {/* Context */}
          {approval.context && (
            <div className="mt-3 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
              <div className="text-sm space-y-2">
                {approval.context.score !== undefined && (
                  <div className="flex items-center gap-2">
                    <span className="text-gray-500">AI Confidence Score:</span>
                    <span className="font-medium">{(approval.context.confidence * 100).toFixed(0)}%</span>
                  </div>
                )}
                {approval.context.amount && (
                  <div className="flex items-center gap-2">
                    <DollarSign size={14} className="text-green-500" />
                    <span className="font-medium">
                      {new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(approval.context.amount)}
                    </span>
                  </div>
                )}
                {approval.context.reasoning && (
                  <div>
                    <span className="text-gray-500">AI Reasoning:</span>
                    <p className="mt-1 text-gray-700 dark:text-gray-300">{approval.context.reasoning}</p>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Decision info */}
          {approval.status !== 'pending' && approval.decidedAt && (
            <div className="mt-3 text-sm text-gray-500">
              {approval.status === 'approved' ? 'Approved' : 'Rejected'}{' '}
              {formatDistanceToNow(new Date(approval.decidedAt), { addSuffix: true })}
              {approval.decisionReason && (
                <span> - "{approval.decisionReason}"</span>
              )}
            </div>
          )}

          {/* Actions */}
          {approval.status === 'pending' && (
            <div className="mt-4 flex items-center gap-3">
              {!showRejectReason ? (
                <>
                  <button
                    onClick={() => onApprove()}
                    disabled={isLoading}
                    className="btn btn-primary disabled:opacity-50"
                  >
                    <CheckCircle size={16} className="mr-2" />
                    Approve
                  </button>
                  <button
                    onClick={() => setShowRejectReason(true)}
                    disabled={isLoading}
                    className="btn btn-danger disabled:opacity-50"
                  >
                    <XCircle size={16} className="mr-2" />
                    Reject
                  </button>
                </>
              ) : (
                <div className="flex-1">
                  <textarea
                    value={rejectReason}
                    onChange={(e) => setRejectReason(e.target.value)}
                    placeholder="Reason for rejection (optional)..."
                    className="input w-full mb-2"
                    rows={2}
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={() => setShowRejectReason(false)}
                      className="btn btn-secondary"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => {
                        onReject(rejectReason);
                        setShowRejectReason(false);
                      }}
                      disabled={isLoading}
                      className="btn btn-danger disabled:opacity-50"
                    >
                      Confirm Rejection
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
