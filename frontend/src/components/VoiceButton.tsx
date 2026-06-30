'use client';

import { useCallback, useEffect, useState } from 'react';
import { Mic, MicOff, Loader2, Volume2, X } from 'lucide-react';
import { clsx } from 'clsx';
import { useVoiceInput, getLanguageName } from '@/hooks/useVoiceInput';

interface VoiceButtonProps {
  onTranscript?: (transcript: string, language: string | null) => void;
  className?: string;
  size?: 'sm' | 'md' | 'lg';
  showLanguageBadge?: boolean;
  showConfidence?: boolean;
  disabled?: boolean;
}

export function VoiceButton({
  onTranscript,
  className,
  size = 'md',
  showLanguageBadge = true,
  showConfidence = false,
  disabled = false,
}: VoiceButtonProps) {
  const {
    isRecording,
    isProcessing,
    transcript,
    language,
    confidence,
    error,
    startRecording,
    stopRecording,
    cancelRecording,
    reset,
  } = useVoiceInput();

  const [showError, setShowError] = useState(false);

  // Notify parent when transcript is ready
  useEffect(() => {
    if (transcript && onTranscript) {
      onTranscript(transcript, language);
    }
  }, [transcript, language, onTranscript]);

  // Show error temporarily
  useEffect(() => {
    if (error) {
      setShowError(true);
      const timer = setTimeout(() => setShowError(false), 5000);
      return () => clearTimeout(timer);
    }
  }, [error]);

  const handleClick = useCallback(async () => {
    if (disabled) return;

    if (isRecording) {
      await stopRecording();
    } else if (!isProcessing) {
      reset();
      await startRecording();
    }
  }, [disabled, isRecording, isProcessing, startRecording, stopRecording, reset]);

  const handleCancel = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      cancelRecording();
    },
    [cancelRecording]
  );

  const sizeClasses = {
    sm: 'w-8 h-8',
    md: 'w-10 h-10',
    lg: 'w-12 h-12',
  };

  const iconSizes = {
    sm: 14,
    md: 18,
    lg: 22,
  };

  return (
    <div className="relative inline-flex items-center gap-2">
      <button
        type="button"
        onClick={handleClick}
        disabled={disabled || isProcessing}
        className={clsx(
          'relative flex items-center justify-center rounded-full transition-all duration-200',
          sizeClasses[size],
          isRecording
            ? 'bg-red-500 hover:bg-red-600 text-white animate-pulse'
            : isProcessing
            ? 'bg-gray-200 dark:bg-gray-700 text-gray-500 cursor-wait'
            : 'bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-600 dark:text-gray-300',
          disabled && 'opacity-50 cursor-not-allowed',
          className
        )}
        title={
          isRecording
            ? 'Click to stop recording'
            : isProcessing
            ? 'Processing audio...'
            : 'Click to start voice input'
        }
        aria-label={isRecording ? 'Stop recording' : 'Start voice input'}
      >
        {isProcessing ? (
          <Loader2 size={iconSizes[size]} className="animate-spin" />
        ) : isRecording ? (
          <>
            <Mic size={iconSizes[size]} />
            {/* Recording indicator ring */}
            <span className="absolute inset-0 rounded-full border-2 border-red-400 animate-ping opacity-75" />
          </>
        ) : (
          <Mic size={iconSizes[size]} />
        )}

        {/* Cancel button when recording */}
        {isRecording && (
          <button
            type="button"
            onClick={handleCancel}
            className="absolute -top-1 -right-1 w-4 h-4 bg-gray-800 dark:bg-gray-200 rounded-full flex items-center justify-center shadow"
            title="Cancel recording"
          >
            <X size={10} className="text-white dark:text-gray-800" />
          </button>
        )}
      </button>

      {/* Language badge */}
      {showLanguageBadge && language && !isRecording && !isProcessing && (
        <span className="text-xs px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-200">
          {getLanguageName(language)}
        </span>
      )}

      {/* Confidence indicator */}
      {showConfidence && confidence > 0 && !isRecording && !isProcessing && (
        <span
          className={clsx(
            'text-xs px-2 py-0.5 rounded-full',
            confidence >= 0.8
              ? 'bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-200'
              : confidence >= 0.5
              ? 'bg-yellow-100 dark:bg-yellow-900 text-yellow-700 dark:text-yellow-200'
              : 'bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-200'
          )}
        >
          {Math.round(confidence * 100)}%
        </span>
      )}

      {/* Error tooltip */}
      {showError && error && (
        <div className="absolute top-full mt-2 left-0 z-50 w-64 p-2 text-xs text-red-700 dark:text-red-200 bg-red-50 dark:bg-red-900/50 rounded-lg shadow-lg border border-red-200 dark:border-red-800">
          {error}
        </div>
      )}
    </div>
  );
}

/**
 * Inline voice input with transcript preview
 */
interface VoiceInputFieldProps {
  onTranscript: (transcript: string, language: string | null) => void;
  placeholder?: string;
  className?: string;
}

export function VoiceInputField({
  onTranscript,
  placeholder = 'Click mic to speak...',
  className,
}: VoiceInputFieldProps) {
  const {
    isRecording,
    isProcessing,
    transcript,
    language,
    confidence,
    error,
    latency,
    startRecording,
    stopRecording,
    cancelRecording,
    reset,
  } = useVoiceInput();

  const handleVoiceClick = useCallback(async () => {
    if (isRecording) {
      await stopRecording();
    } else {
      reset();
      await startRecording();
    }
  }, [isRecording, startRecording, stopRecording, reset]);

  useEffect(() => {
    if (transcript) {
      onTranscript(transcript, language);
    }
  }, [transcript, language, onTranscript]);

  return (
    <div className={clsx('flex items-center gap-3', className)}>
      <button
        type="button"
        onClick={handleVoiceClick}
        disabled={isProcessing}
        className={clsx(
          'flex-shrink-0 w-10 h-10 rounded-full flex items-center justify-center transition-all',
          isRecording
            ? 'bg-red-500 text-white animate-pulse'
            : isProcessing
            ? 'bg-gray-200 dark:bg-gray-700 text-gray-400'
            : 'bg-indigo-100 dark:bg-indigo-900 text-indigo-600 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-800'
        )}
        aria-label={isRecording ? 'Stop recording' : 'Start voice input'}
      >
        {isProcessing ? (
          <Loader2 size={20} className="animate-spin" />
        ) : isRecording ? (
          <Volume2 size={20} />
        ) : (
          <Mic size={20} />
        )}
      </button>

      <div className="flex-1 min-w-0">
        {transcript ? (
          <div className="space-y-1">
            <p className="text-sm text-gray-900 dark:text-white truncate">{transcript}</p>
            <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              {language && <span>{getLanguageName(language)}</span>}
              {confidence > 0 && <span>• {Math.round(confidence * 100)}% confident</span>}
              {latency && <span>• {Math.round(latency.total_ms)}ms</span>}
            </div>
          </div>
        ) : isRecording ? (
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
            <span className="text-sm text-gray-600 dark:text-gray-300">Listening...</span>
            <button
              type="button"
              onClick={cancelRecording}
              className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            >
              Cancel
            </button>
          </div>
        ) : isProcessing ? (
          <span className="text-sm text-gray-500 dark:text-gray-400">Processing audio...</span>
        ) : error ? (
          <span className="text-sm text-red-500">{error}</span>
        ) : (
          <span className="text-sm text-gray-400 dark:text-gray-500">{placeholder}</span>
        )}
      </div>
    </div>
  );
}
