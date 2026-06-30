'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { customersApi } from '@/lib/api';
import { CustomerTimeline } from '@/components/CustomerTimeline';

export default function CustomerDetailPage({ params }: { params: { id: string } }) {
  const customerId = params.id;

  const customerQuery = useQuery({
    queryKey: ['customer', customerId],
    queryFn: () => customersApi.get(customerId),
    enabled: Boolean(customerId),
  });

  const customer = customerQuery.data?.data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm text-gray-500 dark:text-gray-400">
            <Link href="/customers" className="hover:underline">
              Customers
            </Link>
            <span className="mx-2">/</span>
            <span>{customerId}</span>
          </div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">{customer?.name || 'Customer'}</h1>
          {customer?.company && <div className="text-sm text-gray-500 dark:text-gray-400">{customer.company}</div>}
        </div>
      </div>

      <CustomerTimeline customerId={customerId} />
    </div>
  );
}

