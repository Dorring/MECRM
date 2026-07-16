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

  const evidence = Array.isArray(decision.retrievalEvidence) ? decision.retrievalEvidence : [];
  const toolCalls = Array.isArray(decision.toolCalls || decision.tool_calls) ? (decision.toolCalls || decision.tool_calls) : [];
  const policyDecision = decision.policyDecision || {};
  const approval = decision.approval || {};
  const outputValidation = decision.outputValidation || {};

  return (
    <div className="card p-6 space-y-4">
      <div className="space-y-1">
        <div className="text-sm text-gray-500 dark:text-gray-400">Safe agent run evidence</div>
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

      <Section title="Run status">
        <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
          <div><dt className="text-gray-500">Policy</dt><dd>{String(policyDecision.status || 'not recorded')}</dd></div>
          <div><dt className="text-gray-500">Approval</dt><dd>{String(approval.status || 'not required')}</dd></div>
          <div><dt className="text-gray-500">Output validation</dt><dd>{String(outputValidation.status || 'not recorded')}</dd></div>
        </dl>
      </Section>

      {evidence.length > 0 && (
        <Section title="Evidence">
          <ul className="space-y-1 text-sm">
            {evidence.map((item: { type?: string; sourceId?: string }, index: number) => (
              <li key={`${item.type || 'evidence'}-${index}`}>{String(item.type || 'evidence')}: {String(item.sourceId || 'recorded')}</li>
            ))}
          </ul>
        </Section>
      )}

      {toolCalls.length > 0 && (
        <Section title="Tool calls">
          <ul className="space-y-1 text-sm">
            {toolCalls.map((item: { name?: string; outcome?: string }, index: number) => (
              <li key={`${item.name || 'tool'}-${index}`}>{String(item.name || 'tool')}: {String(item.outcome || 'recorded')}</li>
            ))}
          </ul>
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
