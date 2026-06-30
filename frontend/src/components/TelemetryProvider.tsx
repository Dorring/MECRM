'use client';

import React, { Component, createContext, useContext } from 'react';
import { api } from '@/lib/api';

type TelemetryContextValue = {
  notify: (payload: { message: string; stack?: string }) => void;
};

const TelemetryContext = createContext<TelemetryContextValue | null>(null);

export class ErrorBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error) {
    console.error('React error boundary caught', error);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="p-6 text-center text-red-600">
          Something went wrong. Please refresh the page.
        </div>
      );
    }
    return this.props.children;
  }
}

export function TelemetryProvider({ children }: { children: React.ReactNode }) {
  const notify = (payload: { message: string; stack?: string }) => {
    // Placeholder for future backend telemetry endpoint
    console.warn('Telemetry event', payload);
  };

  return (
    <TelemetryContext.Provider value={{ notify }}>
      <ErrorBoundary>{children}</ErrorBoundary>
    </TelemetryContext.Provider>
  );
}

export function useTelemetry() {
  const ctx = useContext(TelemetryContext);
  if (!ctx) throw new Error('useTelemetry must be used within TelemetryProvider');
  return ctx;
}
