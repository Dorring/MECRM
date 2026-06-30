'use client';

import { useMemo } from 'react';

export function ExplainabilityPanel({ decision }: { decision: any }) {
  const header = useMemo(() => {
    if (!decision) return null;
    return {
      actionType: decision.actionType || decision.action_type || decision.type,
      agentId: decision.agentId || decision.agent_id,
      status: decision.status,
      riskLevel: decision.riskLevel || decision.risk_level,
      confidence: decision.confidence,
      createdAt: decision.createdAt || decision.created_at,
      correlationId: decision.correlationId || decision.correlation_id,
    };
  }, [decision]);

  if (!decision) {
    return (
      <div className="card p-6">
        <div className="text-gray-500">Select a decision to view explainability.</div>
      </div>
    );
  }

  const reasoning = decision.reasoning || {};
  const factors = Array.isArray(reasoning.factors) ? reasoning.factors : [];
  const evidence = Array.isArray(decision.evidence) ? decision.evidence : [];
  const toolCalls = Array.isArray(decision.toolCalls || decision.tool_calls) ? (decision.toolCalls || decision.tool_calls) : [];

  return (
    <div className="card p-6 space-y-4">
      <div className="space-y-1">
        <div className="text-sm text-gray-500 dark:text-gray-400">Explainability</div>
        <div className="text-lg font-semibold text-gray-900 dark:text-white">
          {String(header?.actionType || 'Decision')}
        </div>
        <div className="text-sm text-gray-600 dark:text-gray-300">
          Agent: {String(header?.agentId || 'unknown')} · Status: {String(header?.status || 'unknown')} · Risk:{' '}
          {String(header?.riskLevel || 'unknown')}
        </div>
        <div className="text-xs text-gray-500 dark:text-gray-400">
          {header?.createdAt ? `Created: ${String(header.createdAt)}` : null}
          {header?.correlationId ? ` · Correlation: ${String(header.correlationId)}` : null}
          {header?.confidence !== null && header?.confidence !== undefined ? ` · Confidence: ${String(header.confidence)}` : null}
        </div>
      </div>

      {factors.length > 0 && (
        <Section title="Reasoning factors">
          <pre className="text-xs overflow-auto bg-gray-50 dark:bg-gray-900 p-3 rounded-lg">
            {JSON.stringify(factors, null, 2)}
          </pre>
        </Section>
      )}

      {reasoning && Object.keys(reasoning).length > 0 && (
        <Section title="Reasoning chain">
          <pre className="text-xs overflow-auto bg-gray-50 dark:bg-gray-900 p-3 rounded-lg">
            {JSON.stringify(reasoning, null, 2)}
          </pre>
        </Section>
      )}

      {evidence.length > 0 && (
        <Section title="Evidence">
          <pre className="text-xs overflow-auto bg-gray-50 dark:bg-gray-900 p-3 rounded-lg">
            {JSON.stringify(evidence, null, 2)}
          </pre>
        </Section>
      )}

      {toolCalls.length > 0 && (
        <Section title="Tool calls">
          <pre className="text-xs overflow-auto bg-gray-50 dark:bg-gray-900 p-3 rounded-lg">
            {JSON.stringify(toolCalls, null, 2)}
          </pre>
        </Section>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium text-gray-900 dark:text-white">{title}</div>
      {children}
    </div>
  );
}

