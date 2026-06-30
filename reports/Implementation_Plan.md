# Implementation Plan: Enterprise CRM Bug Fixes

## Overview

This plan addresses all 10 bugs identified during comprehensive testing. Fixes are organized by priority and designed to make the CRM enterprise-ready.

---

## P0 - Critical (Block Release)

### 1. Fix SQL Injection Vulnerability

**Files to modify:**
- [gateway/src/routes/auth.ts](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/gateway/src/routes/auth.ts)

**Changes:**

Replace all occurrences of `$executeRawUnsafe` with tagged template literals:

```diff
- await db.$executeRawUnsafe(`SET LOCAL app.tenant_id = '${SYSTEM_TENANT_ID}'`);
+ await db.$executeRaw`SET LOCAL app.tenant_id = ${SYSTEM_TENANT_ID}`;

- await db.$executeRawUnsafe(`SET LOCAL app.tenant_id = '${tenant.id}'`);
+ await db.$executeRaw`SET LOCAL app.tenant_id = ${tenant.id}`;

- await db.$executeRawUnsafe(`SET LOCAL app.tenant_id = '${tenantId}'`);
+ await db.$executeRaw`SET LOCAL app.tenant_id = ${tenantId}`;
```

**Verification:**
```bash
# Search for any remaining unsafe queries
grep -r "\$executeRawUnsafe" gateway/src/
# Should return empty
```

---

### 2. Fix API Base URL Configuration

**Files to modify:**
- [frontend/src/lib/api.ts](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/lib/api.ts)

**Changes:**

```diff
- const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
+ const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:4000';
```

---

### 3. Fix Missing API Route Prefix

**Files to modify:**
- [frontend/src/lib/api.ts](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/lib/api.ts)

**Changes:**

Update all API endpoint calls to include `/api/v1`:

```diff
- export const leadsApi = createServiceApi('/leads');
+ export const leadsApi = createServiceApi('/api/v1/leads');

- export const dealsApi = { ...createServiceApi('/deals'), ... };
+ export const dealsApi = { ...createServiceApi('/api/v1/deals'), ... };

- export const ticketsApi = { ...createServiceApi('/tickets'), ... };
+ export const ticketsApi = { ...createServiceApi('/api/v1/tickets'), ... };

- export const customersApi = { ...createServiceApi('/customers'), ... };
+ export const customersApi = { ...createServiceApi('/api/v1/customers'), ... };

- export const approvalsApi = { ...createServiceApi('/approvals'), ... };
+ export const approvalsApi = { ...createServiceApi('/api/v1/approvals'), ... };

- export const governanceApi = { ...createServiceApi('/governance'), ... };
+ export const governanceApi = { ...createServiceApi('/api/v1/governance'), ... };
```

Also update individual endpoints in other API objects.

**Verification:**
```bash
cd frontend && npm run build
# Should compile without errors
```

---

## P1 - High Priority (Before Production)

### 4. Implement Lead Create Modal

**Files to create:**
- `frontend/src/components/LeadFormModal.tsx` [NEW]

**Files to modify:**
- [frontend/src/app/leads/page.tsx](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/app/leads/page.tsx)

**Implementation:**

Create `LeadFormModal.tsx`:
```typescript
'use client';
import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { leadsApi } from '@/lib/api';

interface LeadFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  lead?: any; // For edit mode
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

  const mutation = useMutation({
    mutationFn: (data: any) => 
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
          <button onClick={onClose}><X size={20} /></button>
        </div>
        <form onSubmit={(e) => { e.preventDefault(); mutation.mutate(formData); }}>
          {/* Form fields */}
          <div className="space-y-4">
            <input className="input w-full" placeholder="Name" 
              value={formData.name} 
              onChange={(e) => setFormData({...formData, name: e.target.value})} 
              required />
            <input className="input w-full" placeholder="Email" type="email"
              value={formData.email}
              onChange={(e) => setFormData({...formData, email: e.target.value})} />
            <input className="input w-full" placeholder="Company"
              value={formData.company}
              onChange={(e) => setFormData({...formData, company: e.target.value})} />
            <input className="input w-full" placeholder="Phone"
              value={formData.phone}
              onChange={(e) => setFormData({...formData, phone: e.target.value})} />
            <select className="input w-full" value={formData.source}
              onChange={(e) => setFormData({...formData, source: e.target.value})}>
              <option value="">Select Source</option>
              <option value="website">Website</option>
              <option value="referral">Referral</option>
              <option value="linkedin">LinkedIn</option>
              <option value="cold_call">Cold Call</option>
            </select>
          </div>
          <div className="flex gap-2 mt-6">
            <button type="button" onClick={onClose} className="btn btn-secondary flex-1">
              Cancel
            </button>
            <button type="submit" className="btn btn-primary flex-1" disabled={mutation.isPending}>
              {mutation.isPending ? 'Saving...' : 'Save'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
```

Update `leads/page.tsx` to import and render the modal.

---

### 5. Add Delete Confirmation Dialog

**Files to create:**
- `frontend/src/components/ConfirmDialog.tsx` [NEW]

**Implementation:**

```typescript
'use client';

interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ 
  isOpen, title, message, confirmLabel = 'Delete', onConfirm, onCancel 
}: ConfirmDialogProps) {
  if (!isOpen) return null;
  
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onCancel} />
      <div className="relative bg-white dark:bg-gray-800 rounded-lg p-6 w-full max-w-sm">
        <h3 className="text-lg font-semibold mb-2">{title}</h3>
        <p className="text-gray-600 dark:text-gray-400 mb-4">{message}</p>
        <div className="flex gap-2">
          <button onClick={onCancel} className="btn btn-secondary flex-1">Cancel</button>
          <button onClick={onConfirm} className="btn btn-danger flex-1">{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
```

---

### 6. Fix Edit Button Handler

**Files to modify:**
- [frontend/src/app/leads/page.tsx](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/app/leads/page.tsx)

**Changes:**

Add edit state and handler:
```typescript
const [editingLead, setEditingLead] = useState<Lead | null>(null);

// In the table row:
<button 
  onClick={() => setEditingLead(lead)}
  className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
>
  <Edit size={16} className="text-gray-500" />
</button>

// Render modal with edit mode:
<LeadFormModal 
  isOpen={isCreateModalOpen || !!editingLead}
  onClose={() => { setIsCreateModalOpen(false); setEditingLead(null); }}
  lead={editingLead}
/>
```

---

### 7. Connect Dashboard to Real API

**Files to modify:**
- [frontend/src/app/page.tsx](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/app/page.tsx)

**Changes:**

Replace static data with API calls:
```typescript
'use client';
import { useQuery } from '@tanstack/react-query';
import { leadsApi, dealsApi, ticketsApi, approvalsApi } from '@/lib/api';

export default function DashboardPage() {
  const leadsQuery = useQuery({ queryKey: ['leads-count'], queryFn: () => leadsApi.list({ limit: 1 }) });
  const dealsQuery = useQuery({ queryKey: ['deals-count'], queryFn: () => dealsApi.list({ limit: 1 }) });
  const ticketsQuery = useQuery({ queryKey: ['tickets-count'], queryFn: () => ticketsApi.list({ limit: 1, status: 'open' }) });
  const approvalsQuery = useQuery({ queryKey: ['approvals-pending'], queryFn: () => approvalsApi.list({ status: 'pending' }) });

  const stats = [
    { name: 'Total Leads', value: leadsQuery.data?.data?.pagination?.total || 0, ... },
    // etc.
  ];
  // ...
}
```

---

## P2 - Medium Priority

### 8. Add TypeScript Interface Definitions

**Files to modify:**
- [frontend/src/lib/api.ts](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/lib/api.ts)

**Changes:**

Replace weak types with proper interfaces:
```typescript
export interface Lead {
  id: string;
  name: string;
  email?: string;
  phone?: string;
  company?: string;
  source?: string;
  status: 'new' | 'contacted' | 'qualified' | 'unqualified' | 'converted';
  score?: number;
  assignedUserId?: string;
  assignedUser?: { id: string; name: string };
  createdAt: string;
  updatedAt: string;
}
```

---

### 9. Add Governance Approval Actions

**Files to modify:**
- [frontend/src/app/governance/page.tsx](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/frontend/src/app/governance/page.tsx)

**Changes:**

Add approve/reject buttons with mutation handlers.

---

### 10. Add Ready Endpoint Dependency Checks

**Files to modify:**
- [gateway/src/index.ts](file:///f:/Dev_Env/Multi-Agent-Enterprise-CRM/gateway/src/index.ts)

**Changes:**
```typescript
app.get('/ready', async (req, res) => {
  try {
    // Check PostgreSQL
    await prisma.$queryRaw`SELECT 1`;
    // Check Redis
    await redisClient.ping();
    // Check Kafka
    const kafkaAdmin = kafkaProducer.admin();
    await kafkaAdmin.connect();
    await kafkaAdmin.disconnect();
    
    res.json({ status: 'ready' });
  } catch (error) {
    res.status(503).json({ status: 'not ready', error: String(error) });
  }
});
```

---

## Verification Checklist

After implementing all fixes:

- [ ] Run `grep -r "\$executeRawUnsafe" gateway/` - should return empty
- [ ] Run `npm run build` in frontend - no errors
- [ ] Test login flow with valid credentials
- [ ] Test lead CRUD operations
- [ ] Test delete with confirmation dialog
- [ ] Verify dashboard shows real data
- [ ] Check `/ready` endpoint reports all dependencies

---

## Estimated Timeline

| Priority | Issues | Time Estimate |
|----------|--------|---------------|
| P0 | 3 critical fixes | 2-3 hours |
| P1 | 4 high priority | 4-6 hours |
| P2 | 3 medium priority | 2-3 hours |
| **Total** | **10 issues** | **8-12 hours** |
