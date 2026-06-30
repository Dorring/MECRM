'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { 
  Bot,
  Activity,
  CheckCircle,
  XCircle,
  Clock,
  Brain,
  MessageSquare,
  TrendingUp,
  Shield,
  RefreshCw
} from 'lucide-react';
import { api } from '@/lib/api';
import { clsx } from 'clsx';
import { format, formatDistanceToNow } from 'date-fns';

interface Agent {
  id: string;
  name: string;
  type: string;
  description: string;
  capabilities: string[];
  isActive: boolean;
}

interface AgentEvent {
  id: string;
  agentId: string;
  eventType: string;
  actionType: string;
  targetEntity: string | null;
  targetId: string | null;
  reasoning: string | null;
  confidence: number | null;
  requiresApproval: boolean;
  isApproved: boolean | null;
  createdAt: string;
}

const agentIcons: Record<string, any> = {
  sales: TrendingUp,
  support: MessageSquare,
  compliance: Shield,
  analytics: Activity,
};

const agentColors: Record<string, string> = {
  sales: 'bg-blue-500',
  support: 'bg-green-500',
  compliance: 'bg-purple-500',
  analytics: 'bg-orange-500',
};

export default function AgentsPage() {
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);

  // Fetch agents
  const { data: agentsData, isLoading: agentsLoading, error: agentsError, refetch: refetchAgents } = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.get<{ data: Agent[] }>('/api/v1/agents'),
  });
  const [agentsTimedOut, setAgentsTimedOut] = useState(false);
  useEffect(() => {
    if (agentsLoading) {
      const id = setTimeout(() => setAgentsTimedOut(true), 10000);
      return () => clearTimeout(id);
    }
    setAgentsTimedOut(false);
  }, [agentsLoading]);

  // Fetch events for selected agent
  const { data: eventsData, isLoading: eventsLoading, error: eventsError, refetch: refetchEvents } = useQuery({
    queryKey: ['agent-events', selectedAgent],
    queryFn: () => api.get<{ data: AgentEvent[] }>(`/api/v1/agents/${selectedAgent}/events`),
    enabled: !!selectedAgent,
  });
  const [eventsTimedOut, setEventsTimedOut] = useState(false);
  useEffect(() => {
    if (eventsLoading) {
      const id = setTimeout(() => setEventsTimedOut(true), 10000);
      return () => clearTimeout(id);
    }
    setEventsTimedOut(false);
  }, [eventsLoading]);

  const agents = agentsData?.data.data || [];
  const events = eventsData?.data.data || [];

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          AI Agents
        </h1>
        <p className="text-gray-500 dark:text-gray-400">
          Monitor and inspect AI agent activities
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Agents list */}
        <div className="lg:col-span-1 space-y-4">
          <h2 className="font-semibold text-gray-900 dark:text-white">
            Registered Agents
          </h2>

          {agentsLoading ? (
            <div className="card p-4 text-center text-gray-500">Loading agents...</div>
          ) : agentsTimedOut || agentsError ? (
            <div className="card p-4 text-center text-red-500 space-y-2">
              <div>{agentsTimedOut ? 'Request timed out' : 'Failed to load agents'}</div>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => {
                  setAgentsTimedOut(false);
                  refetchAgents();
                }}
              >
                Retry
              </button>
            </div>
          ) : (
            agents.map((agent) => {
              const Icon = agentIcons[agent.type] || Bot;
              const color = agentColors[agent.type] || 'bg-gray-500';

              return (
                <button
                  key={agent.id}
                  onClick={() => setSelectedAgent(agent.id)}
                  className={clsx(
                    'w-full card text-left transition-all hover:shadow-md',
                    selectedAgent === agent.id && 'ring-2 ring-primary-500'
                  )}
                >
                  <div className="flex items-start gap-3">
                    <div className={clsx('p-2 rounded-lg', color)}>
                      <Icon size={24} className="text-white" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between">
                        <h3 className="font-medium text-gray-900 dark:text-white">
                          {agent.name}
                        </h3>
                        <span className={clsx(
                          'w-2 h-2 rounded-full',
                          agent.isActive ? 'bg-green-500' : 'bg-gray-400'
                        )} />
                      </div>
                      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                        {agent.description}
                      </p>
                      <div className="flex flex-wrap gap-1 mt-2">
                        {agent.capabilities.slice(0, 3).map((cap) => (
                          <span key={cap} className="badge badge-info text-xs">
                            {cap.split(':')[1]}
                          </span>
                        ))}
                        {agent.capabilities.length > 3 && (
                          <span className="text-xs text-gray-400">
                            +{agent.capabilities.length - 3} more
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </button>
              );
            })
          )}
        </div>

        {/* Agent activity */}
        <div className="lg:col-span-2">
          {selectedAgent ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold text-gray-900 dark:text-white">
                  Recent Activity
                </h2>
                <button
                  className="btn btn-ghost text-sm"
                  onClick={() => selectedAgent && refetchEvents()}
                  disabled={eventsLoading}
                >
                  <RefreshCw size={14} className="mr-2" />
                  Refresh
                </button>
              </div>

              {eventsLoading ? (
                <div className="card p-8 text-center text-gray-500">Loading events...</div>
              ) : eventsTimedOut || eventsError ? (
                <div className="card p-8 text-center text-red-500 space-y-2">
                  <div>{eventsTimedOut ? 'Request timed out' : 'Failed to load events'}</div>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => {
                      setEventsTimedOut(false);
                      refetchEvents();
                    }}
                  >
                    Retry
                  </button>
                </div>
              ) : events.length === 0 ? (
                <div className="card p-8 text-center text-gray-500">No recent activity</div>
              ) : (
                <div className="space-y-3">
                  {events.map((event) => (
                    <EventCard key={event.id} event={event} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="card h-full flex items-center justify-center">
              <div className="text-center">
                <Bot size={48} className="mx-auto text-gray-300 dark:text-gray-600" />
                <p className="mt-4 text-gray-500">
                  Select an agent to view activity
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function EventCard({ event }: { event: AgentEvent }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="card">
      <div
        className="flex items-start gap-3 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        {/* Event type indicator */}
        <div className={clsx(
          'p-2 rounded-lg',
          event.requiresApproval
            ? event.isApproved
              ? 'bg-green-100 dark:bg-green-900'
              : event.isApproved === false
              ? 'bg-red-100 dark:bg-red-900'
              : 'bg-yellow-100 dark:bg-yellow-900'
            : 'bg-blue-100 dark:bg-blue-900'
        )}>
          {event.requiresApproval ? (
            event.isApproved ? (
              <CheckCircle size={20} className="text-green-600" />
            ) : event.isApproved === false ? (
              <XCircle size={20} className="text-red-600" />
            ) : (
              <Clock size={20} className="text-yellow-600" />
            )
          ) : (
            <Activity size={20} className="text-blue-600" />
          )}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between">
            <span className="font-medium text-gray-900 dark:text-white">
              {event.eventType.replace('crm.agents.', '').replace('-', ' ').replace(/_/g, ' ')}
            </span>
            <span className="text-xs text-gray-500">
              {formatDistanceToNow(new Date(event.createdAt), { addSuffix: true })}
            </span>
          </div>

          <div className="flex items-center gap-2 mt-1 text-sm text-gray-500">
            {event.actionType && (
              <span className="badge badge-info text-xs">{event.actionType}</span>
            )}
            {event.targetEntity && (
              <span>{event.targetEntity}: {event.targetId?.slice(0, 8)}...</span>
            )}
            {event.confidence !== null && (
              <span className="flex items-center">
                <Brain size={12} className="mr-1" />
                {(event.confidence * 100).toFixed(0)}%
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Expanded reasoning */}
      {expanded && event.reasoning && (
        <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
          <div className="flex items-start gap-2">
            <Brain size={16} className="text-primary-500 flex-shrink-0 mt-1" />
            <div>
              <div className="text-xs font-medium text-gray-500 uppercase mb-1">
                AI Reasoning
              </div>
              <p className="text-sm text-gray-700 dark:text-gray-300">
                {event.reasoning}
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
