 'use client';
import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { leadsApi, Lead } from '@/lib/api';

interface LeadFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  lead?: Lead | null;
}

export function LeadFormModal({ isOpen, onClose, lead }: LeadFormModalProps) {
  const queryClient = useQueryClient();
  const [formData, setFormData] = useState({
    name: lead?.name || '',
    email: lead?.email || '',
    company: lead?.company || '',
    phone: lead?.phone || '',
    source: lead?.source || '',
  });

  useEffect(() => {
    setFormData({
      name: lead?.name || '',
      email: lead?.email || '',
      company: lead?.company || '',
      phone: lead?.phone || '',
      source: lead?.source || '',
    });
  }, [lead]);

  const mutation = useMutation({
    mutationFn: (data: typeof formData) =>
      lead ? leadsApi.update(lead.id, data) : leadsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['leads'] });
      onClose();
    },
  });

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-white dark:bg-gray-800 rounded-lg p-6 w-full max-w-md">
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold">
            {lead ? 'Edit Lead' : 'Create Lead'}
          </h2>
          <button onClick={onClose} aria-label="Close">
            <X size={20} />
          </button>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!formData.name.trim()) return;
            mutation.mutate(formData);
          }}
          className="space-y-4"
        >
          <input
            className="input w-full"
            placeholder="Name"
            value={formData.name}
            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
            required
          />
          <input
            className="input w-full"
            placeholder="Email"
            type="email"
            value={formData.email}
            onChange={(e) => setFormData({ ...formData, email: e.target.value })}
          />
          <input
            className="input w-full"
            placeholder="Company"
            value={formData.company}
            onChange={(e) => setFormData({ ...formData, company: e.target.value })}
          />
          <input
            className="input w-full"
            placeholder="Phone"
            value={formData.phone}
            onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
          />
          <select
            className="input w-full"
            value={formData.source}
            onChange={(e) => setFormData({ ...formData, source: e.target.value })}
          >
            <option value="">Select Source</option>
            <option value="website">Website</option>
            <option value="referral">Referral</option>
            <option value="linkedin">LinkedIn</option>
            <option value="cold_call">Cold Call</option>
          </select>
          <div className="flex gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="btn btn-secondary flex-1"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary flex-1"
              disabled={mutation.isPending}
            >
              {mutation.isPending ? 'Saving...' : 'Save'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
