'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { 
  Plus, 
  Search, 
  Filter, 
  MoreVertical,
  User,
  Building,
  Mail,
  Phone,
  Star,
  Edit,
  Trash2,
  Bot
} from 'lucide-react';
import { leadsApi, Lead, predictionsApi } from '@/lib/api';
import { clsx } from 'clsx';
import { RiskBadges } from '@/components/RiskBadges';
import { LeadFormModal } from '@/components/LeadFormModal';
import { ConfirmDialog } from '@/components/ConfirmDialog';

const statusColors: Record<string, string> = {
  new: 'badge-info',
  contacted: 'badge-warning',
  qualified: 'badge-success',
  unqualified: 'badge-danger',
  converted: 'badge-success',
};

const statusLabels: Record<string, string> = {
  new: 'New',
  contacted: 'Contacted',
  qualified: 'Qualified',
  unqualified: 'Unqualified',
  converted: 'Converted',
};

export default function LeadsPage() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [editingLead, setEditingLead] = useState<Lead | null>(null);
  const [leadToDelete, setLeadToDelete] = useState<Lead | null>(null);

  // Fetch leads
  const { data, isLoading, error } = useQuery({
    queryKey: ['leads', page, statusFilter],
    queryFn: () => leadsApi.list({ page, limit: 20, status: statusFilter || undefined }),
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => leadsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['leads'] });
      setLeadToDelete(null);
    },
  });

  const leads = data?.data.data || [];
  const pagination = data?.data.pagination;

  // Filter leads by search query
  const filteredLeads = searchQuery
    ? leads.filter(
        (lead) =>
          lead.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
          lead.email?.toLowerCase().includes(searchQuery.toLowerCase()) ||
          lead.company?.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : leads;

  const predictionsQuery = useQuery({
    queryKey: ['predictionsLatest', 'lead', filteredLeads.map((l) => l.id).join(',')],
    queryFn: () => predictionsApi.latest('lead', filteredLeads.map((l) => l.id)),
    enabled: filteredLeads.length > 0,
  });
  const predictionsByLeadId = predictionsQuery.data?.data?.data || {};

  return (
    <>
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Leads
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Manage and track your sales leads
          </p>
        </div>
        <button
          onClick={() => setIsCreateModalOpen(true)}
          className="btn btn-primary"
        >
          <Plus size={20} className="mr-2" />
          Add Lead
        </button>
      </div>

      {/* Filters */}
      <div className="card">
        <div className="flex flex-col sm:flex-row gap-4">
          {/* Search */}
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={20} />
            <input
              type="text"
              placeholder="Search leads..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="input pl-10 w-full"
            />
          </div>

          {/* Status filter */}
          <div className="flex items-center gap-2">
            <Filter size={20} className="text-gray-400" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="input"
            >
              <option value="">All Statuses</option>
              <option value="new">New</option>
              <option value="contacted">Contacted</option>
              <option value="qualified">Qualified</option>
              <option value="unqualified">Unqualified</option>
              <option value="converted">Converted</option>
            </select>
          </div>
        </div>
      </div>

      {/* Leads table */}
      <div className="card overflow-hidden p-0">
        {isLoading ? (
          <div className="p-8 text-center text-gray-500">Loading leads...</div>
        ) : error ? (
          <div className="p-8 text-center text-red-500">Failed to load leads</div>
        ) : filteredLeads.length === 0 ? (
          <div className="p-8 text-center text-gray-500">No leads found</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-50 dark:bg-gray-800">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Lead
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Company
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Status
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Score
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Source
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Assigned To
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {filteredLeads.map((lead) => (
                  <tr
                    key={lead.id}
                    className="hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                  >
                    <td className="px-6 py-4">
                      <div className="flex items-center">
                        <div className="w-10 h-10 rounded-full bg-primary-100 dark:bg-primary-900 flex items-center justify-center">
                          <User size={20} className="text-primary-600" />
                        </div>
                        <div className="ml-4">
                          <div className="text-sm font-medium text-gray-900 dark:text-white">
                            {lead.name}
                          </div>
                          <div className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
                            {lead.email && (
                              <span className="flex items-center gap-1">
                                <Mail size={12} />
                                {lead.email}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center text-sm text-gray-900 dark:text-white">
                        {lead.company ? (
                          <>
                            <Building size={16} className="mr-2 text-gray-400" />
                            {lead.company}
                          </>
                        ) : (
                          <span className="text-gray-400">—</span>
                        )}
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      <span className={clsx('badge', statusColors[lead.status])}>
                        {statusLabels[lead.status] || lead.status}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      {lead.score !== null ? (
                        <div className="flex items-center">
                          <div className="flex items-center gap-1">
                            <Star
                              size={16}
                              className={clsx(
                                lead.score >= 70
                                  ? 'text-yellow-500 fill-yellow-500'
                                  : 'text-gray-300'
                              )}
                            />
                            <span className="text-sm font-medium">{lead.score}</span>
                          </div>
                          <span className="ml-2 inline-flex" title="AI-scored" aria-label="AI-scored">
                            <Bot size={14} className="text-primary-500" />
                          </span>
                          <span className="ml-2">
                            <RiskBadges predictions={predictionsByLeadId[lead.id] || null} />
                          </span>
                        </div>
                      ) : (
                        <span className="text-gray-400 text-sm">Not scored</span>
                      )}
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-500 dark:text-gray-400">
                      {lead.source || '—'}
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-900 dark:text-white">
                      {lead.assignedUser?.name || (
                        <span className="text-gray-400">Unassigned</span>
                      )}
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => setEditingLead(lead)}
                          className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                        >
                          <Edit size={16} className="text-gray-500" />
                        </button>
                        <button
                          onClick={() => setLeadToDelete(lead)}
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
        )}

        {/* Pagination */}
        {pagination && pagination.totalPages > 1 && (
          <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
            <div className="text-sm text-gray-500 dark:text-gray-400">
              Showing {(page - 1) * pagination.limit + 1} to{' '}
              {Math.min(page * pagination.limit, pagination.total)} of {pagination.total} leads
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
      </div>
    </div>

    <LeadFormModal
      isOpen={isCreateModalOpen || !!editingLead}
      onClose={() => {
        setIsCreateModalOpen(false);
        setEditingLead(null);
      }}
      lead={editingLead}
    />

    <ConfirmDialog
      isOpen={!!leadToDelete}
      title="Delete lead"
      message={`Are you sure you want to delete ${leadToDelete?.name || 'this lead'}?`}
      onCancel={() => setLeadToDelete(null)}
      onConfirm={() => {
        if (leadToDelete) deleteMutation.mutate(leadToDelete.id);
      }}
    />

    </>
  );
}
