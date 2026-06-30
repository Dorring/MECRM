'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import {
  Search,
  User,
  Building,
  Mail,
  Phone,
  DollarSign,
  Edit,
  Trash2,
  ExternalLink
} from 'lucide-react';
import { customersApi, Customer, predictionsApi } from '@/lib/api';
import { clsx } from 'clsx';
import { format } from 'date-fns';
import { RiskBadges } from '@/components/RiskBadges';
import { ConfirmDialog } from '@/components/ConfirmDialog';

const segmentColors: Record<string, string> = {
  enterprise: 'badge-success',
  mid_market: 'badge-info',
  smb: 'badge-warning',
  startup: 'badge bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-300',
};

export default function CustomersPage() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [segmentFilter, setSegmentFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');
  const [customerToDelete, setCustomerToDelete] = useState<Customer | null>(null);

  // Fetch customers
  const { data, isLoading, error } = useQuery({
    queryKey: ['customers', page, segmentFilter],
    queryFn: () => customersApi.list({ page, limit: 20, segment: segmentFilter || undefined }),
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => customersApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customers'] });
      setCustomerToDelete(null);
    },
  });

  const customers = data?.data.data || [];
  const pagination = data?.data.pagination;

  // Filter by search
  const filteredCustomers = searchQuery
    ? customers.filter(
        (customer) =>
          customer.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
          customer.email?.toLowerCase().includes(searchQuery.toLowerCase()) ||
          customer.company?.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : customers;

  const predictionsQuery = useQuery({
    queryKey: ['predictionsLatest', 'customer', filteredCustomers.map((c) => c.id).join(',')],
    queryFn: () => predictionsApi.latest('customer', filteredCustomers.map((c) => c.id)),
    enabled: filteredCustomers.length > 0,
  });

  const predictionsByCustomerId = predictionsQuery.data?.data?.data || {};

  // Stats
  const totalLTV = customers.reduce((sum, c) => sum + c.lifetimeValue, 0);

  const formatCurrency = (amount: number) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Customers
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Total LTV: <span className="font-semibold text-primary-600">{formatCurrency(totalLTV)}</span>
          </p>
        </div>
        {/* TODO(Phase 5): "Add Customer" form/modal does not exist yet.
            Hidden to avoid shipping a dead button. Wire to a CustomerFormModal
            once the create-customer field contract is defined with the gateway. */}
      </div>

      {/* Filters */}
      <div className="card">
        <div className="flex flex-col sm:flex-row gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={20} />
            <input
              type="text"
              placeholder="Search customers..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="input pl-10 w-full"
            />
          </div>

          <select
            value={segmentFilter}
            onChange={(e) => setSegmentFilter(e.target.value)}
            className="input"
          >
            <option value="">All Segments</option>
            <option value="enterprise">Enterprise</option>
            <option value="mid_market">Mid-Market</option>
            <option value="smb">SMB</option>
            <option value="startup">Startup</option>
          </select>
        </div>
      </div>

      {/* Customer grid */}
      {isLoading ? (
        <div className="card p-8 text-center text-gray-500">Loading customers...</div>
      ) : error ? (
        <div className="card p-8 text-center text-red-500">Failed to load customers</div>
      ) : filteredCustomers.length === 0 ? (
        <div className="card p-8 text-center text-gray-500">No customers found</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredCustomers.map((customer) => (
            <CustomerCard
              key={customer.id}
              customer={customer}
              predictions={predictionsByCustomerId[customer.id] || null}
              onDelete={() => setCustomerToDelete(customer)}
            />
          ))}
        </div>
      )}

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
        isOpen={!!customerToDelete}
        title="Delete customer"
        message={`Are you sure you want to delete ${customerToDelete?.name || 'this customer'}?`}
        onCancel={() => setCustomerToDelete(null)}
        onConfirm={() => {
          if (customerToDelete) deleteMutation.mutate(customerToDelete.id);
        }}
      />
    </div>
  );
}

function CustomerCard({
  customer,
  predictions,
  onDelete,
}: {
  customer: Customer;
  predictions: any;
  onDelete: () => void;
}) {
  const formatCurrency = (amount: number) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);

  return (
    <div className="card hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 rounded-full bg-primary-100 dark:bg-primary-900 flex items-center justify-center">
            <User size={24} className="text-primary-600" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="font-medium text-gray-900 dark:text-white">{customer.name}</h3>
              <RiskBadges predictions={predictions} />
            </div>
            {customer.segment && (
              <span className={clsx('badge text-xs', segmentColors[customer.segment] || 'badge-info')}>
                {customer.segment.replace('_', ' ')}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1">
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

      <div className="mt-4 space-y-2 text-sm">
        {customer.company && (
          <div className="flex items-center text-gray-600 dark:text-gray-400">
            <Building size={14} className="mr-2 text-gray-400" />
            {customer.company}
          </div>
        )}
        {customer.email && (
          <div className="flex items-center text-gray-600 dark:text-gray-400">
            <Mail size={14} className="mr-2 text-gray-400" />
            {customer.email}
          </div>
        )}
        {customer.phone && (
          <div className="flex items-center text-gray-600 dark:text-gray-400">
            <Phone size={14} className="mr-2 text-gray-400" />
            {customer.phone}
          </div>
        )}
      </div>

      <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <div>
          <div className="text-xs text-gray-500">Lifetime Value</div>
          <div className="text-lg font-bold text-primary-600 flex items-center">
            <DollarSign size={16} className="mr-1" />
            {formatCurrency(customer.lifetimeValue)}
          </div>
        </div>
        <Link href={`/customers/${customer.id}`} className="btn btn-ghost text-sm">
          <ExternalLink size={14} className="mr-1" />
          View
        </Link>
      </div>
    </div>
  );
}
