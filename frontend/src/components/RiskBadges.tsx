'use client';

import { clsx } from 'clsx';

type RiskLevel = 'green' | 'yellow' | 'red' | string;

export type PredictionItem = {
  predictionType?: string;
  prediction_type?: string;
  probability?: number;
  riskLevel?: RiskLevel;
  risk_level?: RiskLevel;
  explanation?: string;
};

export function RiskBadges({ predictions }: { predictions?: Record<string, PredictionItem> | PredictionItem[] | null }) {
  const list: PredictionItem[] = Array.isArray(predictions) ? predictions : predictions ? Object.values(predictions) : [];
  if (!list.length) return null;

  const rank = (lvl: RiskLevel) => {
    if (lvl === 'red') return 3;
    if (lvl === 'yellow') return 2;
    if (lvl === 'green') return 1;
    return 0;
  };

  const normalized = list.map((p) => ({
    predictionType: p.predictionType || p.prediction_type || 'risk',
    probability: typeof p.probability === 'number' ? p.probability : 0,
    riskLevel: (p.riskLevel || p.risk_level || 'green') as RiskLevel,
    explanation: p.explanation || '',
  }));

  const worst = normalized.reduce((a, b) => (rank(a.riskLevel) >= rank(b.riskLevel) ? a : b));
  const label = worst.riskLevel === 'red' ? '🔴' : worst.riskLevel === 'yellow' ? '🟡' : '🟢';

  const badgeClass =
    worst.riskLevel === 'red'
      ? 'badge bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
      : worst.riskLevel === 'yellow'
        ? 'badge bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200'
        : 'badge badge-success';

  const title = `${worst.predictionType}${typeof worst.probability === 'number' ? ` · ${(worst.probability * 100).toFixed(0)}%` : ''}${worst.explanation ? ` · ${worst.explanation}` : ''}`;

  return (
    <span className={clsx('inline-flex items-center gap-2', 'text-xs')} title={title} aria-label={title}>
      <span className={badgeClass}>
        {label} {worst.predictionType}
      </span>
    </span>
  );
}

