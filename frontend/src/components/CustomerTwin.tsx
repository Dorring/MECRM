'use client';

import { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { clsx } from 'clsx';
import { Brain, TrendingUp, TrendingDown, AlertTriangle, Loader2, ChevronDown, Info } from 'lucide-react';

// API client for twins
const twinsApi = {
  getProfile: async (customerId: string) => {
    const res = await fetch(`/api/intelligence/twin/${customerId}`, {
      credentials: 'include',
    });
    if (!res.ok) throw new Error('Failed to fetch twin profile');
    return res.json();
  },
  simulate: async (customerId: string, scenario: string, params?: Record<string, any>) => {
    const res = await fetch('/api/intelligence/twin/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ customer_id: customerId, scenario, params }),
    });
    if (!res.ok) throw new Error('Simulation failed');
    return res.json();
  },
  getScenarios: async () => {
    const res = await fetch('/api/intelligence/twin/scenarios', {
      credentials: 'include',
    });
    if (!res.ok) throw new Error('Failed to fetch scenarios');
    return res.json();
  },
};

type Scenario = {
  id: string;
  label: string;
  category: string;
};

type SimulationResult = {
  success: boolean;
  customer_id: string;
  scenario: string;
  outcomes: Record<string, number>;
  explanation: string;
  factors: string[];
  confidence: number;
  simulated_at: string;
};

type ProbabilityBarProps = {
  label: string;
  value: number;
  color: 'green' | 'yellow' | 'red' | 'blue';
};

function ProbabilityBar({ label, value, color }: ProbabilityBarProps) {
  const colorClasses = {
    green: 'bg-green-500',
    yellow: 'bg-yellow-500',
    red: 'bg-red-500',
    blue: 'bg-blue-500',
  };

  const percentage = Math.round(value * 100);

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-sm">
        <span className="font-medium capitalize text-gray-700 dark:text-gray-300">{label}</span>
        <span className="text-gray-500 dark:text-gray-400">{percentage}%</span>
      </div>
      <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className={clsx('h-full rounded-full transition-all duration-500', colorClasses[color])}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}

function ConfidenceIndicator({ confidence }: { confidence: number }) {
  const percentage = Math.round(confidence * 100);
  const level = confidence >= 0.7 ? 'high' : confidence >= 0.4 ? 'medium' : 'low';
  const colors = {
    high: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
    medium: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
    low: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  };

  return (
    <div className={clsx('inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium', colors[level])}>
      <Brain size={12} />
      <span>{percentage}% confidence</span>
    </div>
  );
}

export function CustomerTwin({ customerId }: { customerId: string }) {
  const [selectedScenario, setSelectedScenario] = useState<string>('');
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const queryClient = useQueryClient();

  // Fetch available scenarios
  const scenariosQuery = useQuery({
    queryKey: ['twinScenarios'],
    queryFn: twinsApi.getScenarios,
  });

  // Mutation for running simulation
  const simulationMutation = useMutation({
    mutationFn: ({ scenario }: { scenario: string }) => twinsApi.simulate(customerId, scenario),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['twinSimulation', customerId] });
    },
  });

  const scenarios = useMemo(() => {
    return (scenariosQuery.data?.scenarios || []) as Scenario[];
  }, [scenariosQuery.data]);

  const selectedScenarioLabel = scenarios.find((s) => s.id === selectedScenario)?.label || 'Select scenario';

  const handleRunSimulation = () => {
    if (selectedScenario) {
      simulationMutation.mutate({ scenario: selectedScenario });
    }
  };

  const result = simulationMutation.data as SimulationResult | undefined;

  // Color mapping for outcomes
  const getOutcomeColor = (key: string): 'green' | 'yellow' | 'red' | 'blue' => {
    const positiveKeys = ['retain', 'renew', 'accept'];
    const negativeKeys = ['churn', 'decline'];
    const neutralKeys = ['negotiate', 'defer', 'complain'];

    if (positiveKeys.includes(key)) return 'green';
    if (negativeKeys.includes(key)) return 'red';
    if (neutralKeys.includes(key)) return 'yellow';
    return 'blue';
  };

  return (
    <div className="card space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain className="w-5 h-5 text-purple-500" />
          <h3 className="font-semibold text-gray-900 dark:text-white">Customer Twin</h3>
        </div>
        {result && <ConfidenceIndicator confidence={result.confidence} />}
      </div>

      {/* Scenario Selector */}
      <div className="relative">
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Simulation Scenario
        </label>
        <button
          type="button"
          onClick={() => setIsDropdownOpen(!isDropdownOpen)}
          className="w-full flex items-center justify-between px-4 py-2 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg text-left text-sm text-gray-900 dark:text-white hover:border-purple-500 focus:outline-none focus:ring-2 focus:ring-purple-500"
        >
          <span>{selectedScenarioLabel}</span>
          <ChevronDown size={16} className={clsx('transition-transform', isDropdownOpen && 'rotate-180')} />
        </button>

        {isDropdownOpen && (
          <div className="absolute z-10 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg">
            {scenarios.map((scenario) => (
              <button
                key={scenario.id}
                type="button"
                onClick={() => {
                  setSelectedScenario(scenario.id);
                  setIsDropdownOpen(false);
                }}
                className={clsx(
                  'w-full px-4 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-700',
                  selectedScenario === scenario.id && 'bg-purple-50 dark:bg-purple-900/20'
                )}
              >
                <div className="font-medium text-gray-900 dark:text-white">{scenario.label}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 capitalize">{scenario.category}</div>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Run Simulation Button */}
      <button
        type="button"
        onClick={handleRunSimulation}
        disabled={!selectedScenario || simulationMutation.isPending}
        className={clsx(
          'w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors',
          selectedScenario && !simulationMutation.isPending
            ? 'bg-purple-600 text-white hover:bg-purple-700'
            : 'bg-gray-200 dark:bg-gray-700 text-gray-500 cursor-not-allowed'
        )}
      >
        {simulationMutation.isPending ? (
          <>
            <Loader2 size={16} className="animate-spin" />
            Running Simulation...
          </>
        ) : (
          <>
            <Brain size={16} />
            Run Simulation
          </>
        )}
      </button>

      {/* Simulation Results */}
      {result?.success && (
        <div className="space-y-4 pt-4 border-t border-gray-200 dark:border-gray-700">
          <h4 className="font-medium text-gray-900 dark:text-white">Predicted Outcomes</h4>

          {/* Probability Bars */}
          <div className="space-y-3">
            {Object.entries(result.outcomes)
              .sort(([, a], [, b]) => b - a)
              .map(([key, value]) => (
                <ProbabilityBar key={key} label={key} value={value} color={getOutcomeColor(key)} />
              ))}
          </div>

          {/* Explanation */}
          <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-4 space-y-2">
            <div className="flex items-start gap-2">
              <Info size={16} className="text-blue-500 mt-0.5 flex-shrink-0" />
              <p className="text-sm text-gray-700 dark:text-gray-300">{result.explanation}</p>
            </div>
          </div>

          {/* Key Factors */}
          {result.factors && result.factors.length > 0 && (
            <div className="space-y-2">
              <h5 className="text-sm font-medium text-gray-700 dark:text-gray-300">Key Factors</h5>
              <ul className="space-y-1">
                {result.factors.map((factor, idx) => (
                  <li key={idx} className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
                    <span className="w-1.5 h-1.5 bg-purple-500 rounded-full" />
                    {factor}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Simulation Timestamp */}
          <div className="text-xs text-gray-500 dark:text-gray-400">
            Simulated at {new Date(result.simulated_at).toLocaleString()}
          </div>
        </div>
      )}

      {/* Error State */}
      {simulationMutation.isError && (
        <div className="flex items-center gap-2 p-3 bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 rounded-lg text-sm">
          <AlertTriangle size={16} />
          <span>Simulation failed. Please try again.</span>
        </div>
      )}

      {/* Empty State */}
      {!result && !simulationMutation.isPending && (
        <div className="text-center py-6 text-gray-500 dark:text-gray-400">
          <Brain size={32} className="mx-auto mb-2 opacity-50" />
          <p className="text-sm">Select a scenario and run a simulation to see predicted customer behavior.</p>
        </div>
      )}
    </div>
  );
}
