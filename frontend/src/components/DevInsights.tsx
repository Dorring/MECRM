'use client';

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { clsx } from 'clsx';
import {
  AlertTriangle,
  CheckCircle,
  Clock,
  ExternalLink,
  Server,
  Loader2,
  RefreshCw,
  XCircle,
  Zap,
} from 'lucide-react';

// API client for DevX
const devxApi = {
  getInsights: async () => {
    const res = await fetch('/api/intelligence/devx/insights', {
      credentials: 'include',
    });
    if (!res.ok) throw new Error('Failed to fetch insights');
    return res.json();
  },
  acknowledgeInsight: async (id: string) => {
    const res = await fetch(`/api/intelligence/devx/insights/${id}/acknowledge`, {
      method: 'POST',
      credentials: 'include',
    });
    if (!res.ok) throw new Error('Failed to acknowledge');
    return res.json();
  },
  resolveInsight: async (id: string) => {
    const res = await fetch(`/api/intelligence/devx/insights/${id}/resolve`, {
      method: 'POST',
      credentials: 'include',
    });
    if (!res.ok) throw new Error('Failed to resolve');
    return res.json();
  },
};

type Suggestion = {
  action: string;
  priority: number;
  category: string;
  impact: string;
  requires_approval: boolean;
  docs: string | null;
};

type Insight = {
  id: string;
  incident_type: string;
  severity: string;
  confidence: number;
  suspected_services: string[];
  suggested_actions: Suggestion[];
  metadata: Record<string, any>;
  status: string;
  created_at: string;
};

function SeverityBadge({ severity }: { severity: string }) {
  const colors = {
    critical: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
    high: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
    medium: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
    low: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  };

  const icons = {
    critical: XCircle,
    high: AlertTriangle,
    medium: Clock,
    low: Zap,
  };

  const Icon = icons[severity as keyof typeof icons] || AlertTriangle;
  const colorClass = colors[severity as keyof typeof colors] || colors.medium;

  return (
    <span className={clsx('inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium', colorClass)}>
      <Icon size={12} />
      <span className="capitalize">{severity}</span>
    </span>
  );
}

function ConfidenceMeter({ confidence }: { confidence: number }) {
  const percentage = Math.round(confidence * 100);

  return (
    <div className="flex items-center gap-2">
      <div className="w-16 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-purple-500 rounded-full transition-all"
          style={{ width: `${percentage}%` }}
        />
      </div>
      <span className="text-xs text-gray-500 dark:text-gray-400">{percentage}%</span>
    </div>
  );
}

function InsightCard({
  insight,
  onAcknowledge,
  onResolve,
}: {
  insight: Insight;
  onAcknowledge: (id: string) => void;
  onResolve: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="card border border-gray-200 dark:border-gray-700 hover:shadow-md transition-shadow">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <SeverityBadge severity={insight.severity} />
            <span className="text-sm text-gray-500 dark:text-gray-400 capitalize">
              {insight.incident_type.replace(/_/g, ' ')}
            </span>
          </div>
          <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
            <span>Confidence: <ConfidenceMeter confidence={insight.confidence} /></span>
            <span>{new Date(insight.created_at).toLocaleString()}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {insight.status === 'active' && (
            <>
              <button
                type="button"
                onClick={() => onAcknowledge(insight.id)}
                className="px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors"
              >
                Acknowledge
              </button>
              <button
                type="button"
                onClick={() => onResolve(insight.id)}
                className="px-3 py-1.5 text-xs font-medium text-white bg-green-600 rounded-lg hover:bg-green-700 transition-colors"
              >
                Resolve
              </button>
            </>
          )}
          {insight.status === 'acknowledged' && (
            <span className="px-3 py-1.5 text-xs font-medium text-blue-700 dark:text-blue-300 bg-blue-100 dark:bg-blue-900/30 rounded-lg">
              Acknowledged
            </span>
          )}
          {insight.status === 'resolved' && (
            <span className="px-3 py-1.5 text-xs font-medium text-green-700 dark:text-green-300 bg-green-100 dark:bg-green-900/30 rounded-lg flex items-center gap-1">
              <CheckCircle size={12} />
              Resolved
            </span>
          )}
        </div>
      </div>

      {/* Suspected Services */}
      {insight.suspected_services.length > 0 && (
        <div className="mt-4">
          <div className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2 flex items-center gap-1">
            <Server size={12} />
            Suspected Services
          </div>
          <div className="flex flex-wrap gap-2">
            {insight.suspected_services.map((service) => (
              <span
                key={service}
                className="px-2 py-1 text-xs bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded"
              >
                {service}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Expand Toggle */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="mt-4 text-sm text-purple-600 dark:text-purple-400 hover:underline"
      >
        {expanded ? 'Hide details' : 'Show suggested actions'}
      </button>

      {/* Suggested Actions (Expanded) */}
      {expanded && insight.suggested_actions.length > 0 && (
        <div className="mt-4 space-y-2">
          <div className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2">
            Suggested Actions
          </div>
          {insight.suggested_actions.map((action, idx) => (
            <div
              key={idx}
              className="p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg flex items-start justify-between gap-4"
            >
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs px-1.5 py-0.5 bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300 rounded">
                    P{action.priority}
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400 capitalize">
                    {action.category}
                  </span>
                  {action.requires_approval && (
                    <span className="text-xs px-1.5 py-0.5 bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300 rounded">
                      Requires Approval
                    </span>
                  )}
                </div>
                <p className="text-sm text-gray-900 dark:text-white">{action.action}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Impact: {action.impact}
                </p>
              </div>
              {action.docs && (
                <a
                  href={action.docs}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-purple-600 dark:text-purple-400 hover:text-purple-700"
                >
                  <ExternalLink size={14} />
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function DevInsights() {
  const queryClient = useQueryClient();

  const insightsQuery = useQuery({
    queryKey: ['devxInsights'],
    queryFn: devxApi.getInsights,
    refetchInterval: 30000, // Refresh every 30 seconds
  });

  const acknowledgeMutation = useMutation({
    mutationFn: devxApi.acknowledgeInsight,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['devxInsights'] });
    },
  });

  const resolveMutation = useMutation({
    mutationFn: devxApi.resolveInsight,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['devxInsights'] });
    },
  });

  const insights = (insightsQuery.data?.insights || []) as Insight[];

  const activeInsights = insights.filter((i) => i.status === 'active');
  const acknowledgedInsights = insights.filter((i) => i.status === 'acknowledged');
  const resolvedInsights = insights.filter((i) => i.status === 'resolved');

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <Zap className="w-5 h-5 text-purple-500" />
            Dev Insights
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            AI-powered operational insights and recommendations
          </p>
        </div>
        <button
          type="button"
          onClick={() => insightsQuery.refetch()}
          disabled={insightsQuery.isFetching}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
        >
          <RefreshCw size={14} className={clsx(insightsQuery.isFetching && 'animate-spin')} />
          Refresh
        </button>
      </div>

      {/* Loading State */}
      {insightsQuery.isLoading && (
        <div className="flex items-center justify-center py-12">
          <Loader2 size={24} className="animate-spin text-purple-500" />
        </div>
      )}

      {/* Error State */}
      {insightsQuery.isError && (
        <div className="p-4 bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 rounded-lg">
          Failed to load insights. Please try again.
        </div>
      )}

      {/* Empty State */}
      {!insightsQuery.isLoading && insights.length === 0 && (
        <div className="text-center py-12">
          <CheckCircle className="w-12 h-12 mx-auto text-green-500 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-white">All Systems Healthy</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            No active incidents or anomalies detected.
          </p>
        </div>
      )}

      {/* Active Insights */}
      {activeInsights.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3 flex items-center gap-2">
            <AlertTriangle size={14} className="text-red-500" />
            Active Incidents ({activeInsights.length})
          </h3>
          <div className="space-y-4">
            {activeInsights.map((insight) => (
              <InsightCard
                key={insight.id}
                insight={insight}
                onAcknowledge={(id) => acknowledgeMutation.mutate(id)}
                onResolve={(id) => resolveMutation.mutate(id)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Acknowledged Insights */}
      {acknowledgedInsights.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3 flex items-center gap-2">
            <Clock size={14} className="text-blue-500" />
            In Progress ({acknowledgedInsights.length})
          </h3>
          <div className="space-y-4">
            {acknowledgedInsights.map((insight) => (
              <InsightCard
                key={insight.id}
                insight={insight}
                onAcknowledge={(id) => acknowledgeMutation.mutate(id)}
                onResolve={(id) => resolveMutation.mutate(id)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Recent Resolved (collapsed by default) */}
      {resolvedInsights.length > 0 && (
        <details className="group">
          <summary className="text-sm font-medium text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300 flex items-center gap-2">
            <CheckCircle size={14} className="text-green-500" />
            Recently Resolved ({resolvedInsights.length})
          </summary>
          <div className="mt-4 space-y-4">
            {resolvedInsights.map((insight) => (
              <InsightCard
                key={insight.id}
                insight={insight}
                onAcknowledge={(id) => acknowledgeMutation.mutate(id)}
                onResolve={(id) => resolveMutation.mutate(id)}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
