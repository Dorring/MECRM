'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutDashboard,
  Users,
  Briefcase,
  Ticket,
  Bot,
  BookOpen,
  Shield,
  ShieldCheck,
  Zap,
  Settings,
  History,
  Inbox,
  ChevronLeft,
  ChevronRight
} from 'lucide-react';
import { useState } from 'react';
import { clsx } from 'clsx';
import { useAuth } from '@/app/providers';

const navigation = [
  { name: 'Dashboard', href: '/', icon: LayoutDashboard },
  { name: 'Leads', href: '/leads', icon: Users },
  { name: 'Deals', href: '/deals', icon: Briefcase },
  { name: 'Tickets', href: '/tickets', icon: Ticket },
  { name: 'Customers', href: '/customers', icon: Users },
  { name: 'Knowledge', href: '/knowledge', icon: BookOpen },
  { name: 'Action Inbox', href: '/productivity', icon: Inbox },
  { name: 'AI Agents', href: '/agents', icon: Bot },
  { name: 'Replay', href: '/replay', icon: History },
  { name: 'Approvals', href: '/approvals', icon: Shield },
  { name: 'Governance', href: '/governance', icon: ShieldCheck },
  { name: 'Automations', href: '/automations', icon: Zap },
  { name: 'Settings', href: '/settings', icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const { user } = useAuth();
  // display-only tenant label; not an authorization source
  const tenantName = user?.tenant?.name || 'Tenant';

  return (
    <div
      className={clsx(
        'flex flex-col bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 transition-all duration-300',
        collapsed ? 'w-16' : 'w-64'
      )}
    >
      {/* Logo */}
      <div className="h-16 flex items-center justify-between px-4 border-b border-gray-200 dark:border-gray-800">
        {!collapsed && (
          <span className="text-xl font-bold text-primary-600">
            Enterprise CRM
          </span>
        )}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
        >
          {collapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-4 space-y-1 px-2">
        {navigation.map((item) => {
          const isActive = pathname === item.href;
          const Icon = item.icon;
          
          return (
            <Link
              key={item.name}
              href={item.href}
              className={clsx(
                'flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/20 dark:text-primary-400'
                  : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
              )}
            >
              <Icon size={20} />
              {!collapsed && <span>{item.name}</span>}
            </Link>
          );
        })}
      </nav>

      {/* Tenant info */}
      {!collapsed && (
        <div className="p-4 border-t border-gray-200 dark:border-gray-800">
          <div className="text-xs text-gray-500 dark:text-gray-400">
            Tenant
          </div>
          <div className="text-sm font-medium text-gray-900 dark:text-white truncate">
            {tenantName}
          </div>
        </div>
      )}
    </div>
  );
}
