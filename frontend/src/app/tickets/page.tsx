'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Search,
  Filter,
  Clock,
  AlertTriangle,
  CheckCircle,
  User,
  MessageSquare,
  Edit,
  Trash2,
  Bot
} from 'lucide-react';
import { ticketsApi, Ticket, predictionsApi } from '@/lib/api';
import { clsx } from 'clsx';
import { format, formatDistanceToNow, isPast } from 'date-fns';
import { RiskBadges } from '@/components/RiskBadges';
import { ConfirmDialog } from '@/components/ConfirmDialog';

const priorityConfig: Record<string, { color: string; label: string }> = {
  low: { color: 'badge-info', label: 'Low' },
  medium: { color: 'badge-warning', label: 'Medium' },
  high: { color: 'badge-danger', label: 'High' },
  urgent: { color: 'bg-red-600 text-white', label: 'Urgent' },
};

const statusConfig: Record<string, { color: string; label: string; icon: any }> = {
  open: { color: 'text-blue-500', label: 'Open', icon: MessageSquare },
  in_progress: { color: 'text-yellow-500', label: 'In Progress', icon: Clock },
  pending: { color: 'text-orange-500', label: 'Pending', icon: AlertTriangle },
  resolved: { color: 'text-green-500', label: 'Resolved', icon: CheckCircle },
  closed: { color: 'text-gray-500', label: 'Closed', icon: CheckCircle },
};

export default function TicketsPage() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [priorityFilter, setPriorityFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');
  const [ticketToDelete, setTicketToDelete] = useState<Ticket | null>(null);

  // Fetch tickets
  const { data, isLoading, error } = useQuery({
    queryKey: ['tickets', page, statusFilter, priorityFilter],
    queryFn: () =>
      ticketsApi.list({
        page,
        limit: 20,
        status: statusFilter || undefined,
        priority: priorityFilter || undefined,
      }),
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<Ticket> }) =>
      ticketsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
  });

  // Resolve mutation
  const resolveMutation = useMutation({
    mutationFn: ({ id, resolution }: { id: string; resolution: string }) =>
      ticketsApi.resolve(id, resolution),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => ticketsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tickets'] });
      setTicketToDelete(null);
    },
  });

  const tickets = data?.data.data || [];
  const pagination = data?.data.pagination;

  // Filter by search
  const filteredTickets = searchQuery
    ? tickets.filter(
        (ticket) =>
          ticket.subject.toLowerCase().includes(searchQuery.toLowerCase()) ||
          ticket.description?.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : tickets;

  const predictionsQuery = useQuery({
    queryKey: ['predictionsLatest', 'ticket', filteredTickets.map((t) => t.id).join(',')],
    queryFn: () => predictionsApi.latest('ticket', filteredTickets.map((t) => t.id)),
    enabled: filteredTickets.length > 0,
  });
  const predictionsByTicketId = predictionsQuery.data?.data?.data || {};

  // Stats
  const openCount = tickets.filter((t) => t.status === 'open').length;
  const breachedCount = tickets.filter(
    (t) => t.slaDueAt && isPast(new Date(t.slaDueAt)) && t.status !== 'resolved'
  ).length;

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Support Tickets
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            <span className="font-medium text-primary-600">{openCount}</span> open tickets
            {breachedCount > 0 && (
              <span className="ml-3 text-red-500">
                <AlertTriangle size={14} className="inline mr-1" />
                {breachedCount} SLA breached
              </span>
            )}
          </p>
        </div>
        {/* TODO(Phase 5): "Create Ticket" form/modal does not exist yet.
            Hidden to avoid shipping a dead button. Wire to a TicketFormModal
            once the create-ticket field contract is defined with the gateway. */}
      </div>

      {/* Filters */}
      <div className="card">
        <div className="flex flex-col sm:flex-row gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={20} />
            <input
              type="text"
              placeholder="Search tickets..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="input pl-10 w-full"
            />
          </div>

          <div className="flex items-center gap-2">
            <Filter size={20} className="text-gray-400" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="input"
            >
              <option value="">All Statuses</option>
              <option value="open">Open</option>
              <option value="in_progress">In Progress</option>
              <option value="pending">Pending</option>
              <option value="resolved">Resolved</option>
              <option value="closed">Closed</option>
            </select>

            <select
              value={priorityFilter}
              onChange={(e) => setPriorityFilter(e.target.value)}
              className="input"
            >
              <option value="">All Priorities</option>
              <option value="urgent">Urgent</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          </div>
        </div>
      </div>

      {/* Tickets list */}
      <div className="space-y-4">
        {isLoading ? (
          <div className="card p-8 text-center text-gray-500">Loading tickets...</div>
        ) : error ? (
          <div className="card p-8 text-center text-red-500">Failed to load tickets</div>
        ) : filteredTickets.length === 0 ? (
          <div className="card p-8 text-center text-gray-500">No tickets found</div>
        ) : (
          filteredTickets.map((ticket) => (
            <TicketCard
              key={ticket.id}
              ticket={ticket}
              predictions={predictionsByTicketId[ticket.id] || null}
              onStatusChange={(status) =>
                updateMutation.mutate({ id: ticket.id, data: { status } })
              }
              onResolve={(resolution) =>
                resolveMutation.mutate({ id: ticket.id, resolution })
              }
              onDelete={() => setTicketToDelete(ticket)}
            />
          ))
        )}
      </div>

      {/* Pagination */}
      {pagination && pagination.totalPages > 1 && (
        <div className="flex items-center justify-between">
          <div className="text-sm text-gray-500">
            Showing {(page - 1) * pagination.limit + 1} to{' '}
            {Math.min(page * pagination.limit, pagination.total)} of {pagination.total}
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
              className="btn btn-secondary disabled:opacity-50"
            >
              Previous
            </button>
            <button
              onClick={() => setPage((p) => Math.min(pagination.totalPages, p + 1))}
              disabled={page === pagination.totalPages}
              className="btn btn-secondary disabled:opacity-50"
            >
              Next
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        isOpen={!!ticketToDelete}
        title="Delete ticket"
        message={`Are you sure you want to delete ${ticketToDelete?.subject || 'this ticket'}?`}
        onCancel={() => setTicketToDelete(null)}
        onConfirm={() => {
          if (ticketToDelete) deleteMutation.mutate(ticketToDelete.id);
        }}
      />
    </div>
  );
}

function TicketCard({
  ticket,
  predictions,
  onStatusChange,
  onResolve,
  onDelete,
}: {
  ticket: Ticket;
  predictions: any;
  onStatusChange: (status: string) => void;
  onResolve: (resolution: string) => void;
  onDelete: () => void;
}) {
  const [showResolve, setShowResolve] = useState(false);
  const [resolution, setResolution] = useState('');

  const priority = priorityConfig[ticket.priority] || priorityConfig.medium;
  const status = statusConfig[ticket.status] || statusConfig.open;
  const StatusIcon = status.icon;

  const isOverdue = ticket.slaDueAt && isPast(new Date(ticket.slaDueAt)) && ticket.status !== 'resolved';

  return (
    <div className={clsx(
      'card hover:shadow-md transition-shadow',
      isOverdue && 'border-red-300 dark:border-red-800'
    )}>
      <div className="flex items-start gap-4">
        {/* Status icon */}
        <div className={clsx('p-2 rounded-lg', status.color.replace('text-', 'bg-').replace('500', '100'))}>
          <StatusIcon size={24} className={status.color} />
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="font-medium text-gray-900 dark:text-white">
                {ticket.subject}
              </h3>
              <div className="flex items-center gap-3 mt-1 text-sm text-gray-500">
                <span className={clsx('badge', priority.color)}>
                  {priority.label}
                </span>
                <RiskBadges predictions={predictions} />
                {ticket.category && (
                  <span className="badge badge-info">{ticket.category}</span>
                )}
                {ticket.customer && (
                  <span className="flex items-center">
                    <User size={12} className="mr-1" />
                    {ticket.customer.name}
                  </span>
                )}
              </div>
            </div>

            {/* SLA indicator */}
            {ticket.slaDueAt && ticket.status !== 'resolved' && (
              <div className={clsx(
                'text-right text-sm',
                isOverdue ? 'text-red-500' : 'text-gray-500'
              )}>
                <Clock size={14} className="inline mr-1" />
                {isOverdue ? 'Overdue' : 'Due'}{' '}
                {formatDistanceToNow(new Date(ticket.slaDueAt), { addSuffix: true })}
              </div>
            )}
          </div>

          {ticket.description && (
            <p className="mt-2 text-sm text-gray-600 dark:text-gray-400 line-clamp-2">
              {ticket.description}
            </p>
          )}

          <div className="flex items-center justify-between mt-4">
            <div className="flex items-center gap-4 text-xs text-gray-500">
              <span>
                Created {formatDistanceToNow(new Date(ticket.createdAt), { addSuffix: true })}
              </span>
              {ticket.assignedUser && (
                <span className="flex items-center">
                  <User size={12} className="mr-1" />
                  {ticket.assignedUser.name}
                </span>
              )}
              {ticket.category === 'AI-triaged' && (
                <span className="flex items-center text-primary-500">
                  <Bot size={12} className="mr-1" />
                  AI Triaged
                </span>
              )}
            </div>

            {/* Actions */}
            <div className="flex items-center gap-2">
              {ticket.status !== 'resolved' && (
                <button
                  onClick={() => setShowResolve(!showResolve)}
                  className="btn btn-secondary text-xs py-1 px-2"
                >
                  Resolve
                </button>
              )}
              <button className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">
                <Edit size={16} className="text-gray-500" />
              </button>
              <button
                onClick={onDelete}
                className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
              >
                <Trash2 size={16} className="text-red-500" />
              </button>
            </div>
          </div>

          {/* Resolution form */}
          {showResolve && (
            <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
              <textarea
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
                placeholder="Enter resolution notes..."
                className="input w-full"
                rows={3}
              />
              <div className="flex justify-end gap-2 mt-2">
                <button
                  onClick={() => setShowResolve(false)}
                  className="btn btn-secondary text-sm"
                >
                  Cancel
                </button>
                <button
                  onClick={() => {
                    onResolve(resolution);
                    setShowResolve(false);
                    setResolution('');
                  }}
                  disabled={!resolution.trim()}
                  className="btn btn-primary text-sm disabled:opacity-50"
                >
                  Mark Resolved
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
