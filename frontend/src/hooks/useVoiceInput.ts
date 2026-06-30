'use client';

import { useCallback, useRef, useState } from 'react';
import { api } from '@/lib/api';

export interface VoiceInputState {
  isRecording: boolean;
  isProcessing: boolean;
  transcript: string;
  language: string | null;
  confidence: number;
  error: string | null;
  latency: {
    stt_ms: number;
    detection_ms: number;
    translation_ms: number;
    total_ms: number;
  } | null;
}

export interface VoiceInputActions {
  startRecording: () => Promise<void>;
  stopRecording: () => Promise<void>;
  cancelRecording: () => void;
  reset: () => void;
}

export type UseVoiceInputResult = VoiceInputState & VoiceInputActions;

const LANGUAGE_NAMES: Record<string, string> = {
  en: 'English',
  hi: 'Hindi',
  ta: 'Tamil',
  te: 'Telugu',
  bn: 'Bengali',
  mr: 'Marathi',
  gu: 'Gujarati',
  kn: 'Kannada',
  ml: 'Malayalam',
  pa: 'Punjabi',
  ur: 'Urdu',
  es: 'Spanish',
  fr: 'French',
  de: 'German',
  zh: 'Chinese',
  ja: 'Japanese',
  ko: 'Korean',
  ar: 'Arabic',
  pt: 'Portuguese',
  ru: 'Russian',
};

export function useVoiceInput(): UseVoiceInputResult {
  const [state, setState] = useState<VoiceInputState>({
    isRecording: false,
    isProcessing: false,
    transcript: '',
    language: null,
    confidence: 0,
    error: null,
    latency: null,
  });

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const reset = useCallback(() => {
    setState({
      isRecording: false,
      isProcessing: false,
      transcript: '',
      language: null,
      confidence: 0,
      error: null,
      latency: null,
    });
  }, []);

  const cancelRecording = useCallback(() => {
    if (mediaRecorderRef.current && state.isRecording) {
      mediaRecorderRef.current.stop();
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
    audioChunksRef.current = [];
    setState((prev) => ({
      ...prev,
      isRecording: false,
      isProcessing: false,
    }));
  }, [state.isRecording]);

  const startRecording = useCallback(async () => {
    try {
      // Check for browser support
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setState((prev) => ({
          ...prev,
          error: 'Voice input is not supported in this browser',
        }));
        return;
      }

      // Request microphone access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: 16000,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });

      streamRef.current = stream;
      audioChunksRef.current = [];

      // Use webm codec if available, fallback to wav
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : 'audio/wav';

      const mediaRecorder = new MediaRecorder(stream, { mimeType });
      mediaRecorderRef.current = mediaRecorder;

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };

      mediaRecorder.start(100); // Collect data every 100ms

      setState((prev) => ({
        ...prev,
        isRecording: true,
        error: null,
        transcript: '',
      }));
    } catch (err: any) {
      const errorMessage =
        err.name === 'NotAllowedError'
          ? 'Microphone access denied. Please allow microphone access.'
          : err.name === 'NotFoundError'
          ? 'No microphone found. Please connect a microphone.'
          : `Failed to start recording: ${err.message}`;

      setState((prev) => ({
        ...prev,
        error: errorMessage,
      }));
    }
  }, []);

  const stopRecording = useCallback(async () => {
    if (!mediaRecorderRef.current || !state.isRecording) {
      return;
    }

    return new Promise<void>((resolve) => {
      const mediaRecorder = mediaRecorderRef.current!;

      mediaRecorder.onstop = async () => {
        // Stop all tracks
        if (streamRef.current) {
          streamRef.current.getTracks().forEach((track) => track.stop());
          streamRef.current = null;
        }

        setState((prev) => ({
          ...prev,
          isRecording: false,
          isProcessing: true,
        }));

        try {
          // Combine audio chunks
          const audioBlob = new Blob(audioChunksRef.current, {
            type: mediaRecorder.mimeType,
          });

          // Check minimum audio length (~0.5 seconds)
          if (audioBlob.size < 5000) {
            setState((prev) => ({
              ...prev,
              isProcessing: false,
              error: 'Recording too short. Please speak for longer.',
            }));
            resolve();
            return;
          }

          // Send to API
          const formData = new FormData();
          formData.append('audio', audioBlob, 'recording.webm');

          const response = await api.post<{
            transcript: string;
            language: string;
            confidence: number;
            duration_seconds: number;
            canonical_query: string;
            latency: {
              stt_ms: number;
              detection_ms: number;
              translation_ms: number;
              total_ms: number;
            };
          }>('/api/intelligence/voice', formData, {
            headers: {
              'Content-Type': 'multipart/form-data',
            },
          });

          const data = response.data;

          setState((prev) => ({
            ...prev,
            isProcessing: false,
            transcript: data.transcript,
            language: data.language,
            confidence: data.confidence,
            latency: data.latency,
            error: null,
          }));
        } catch (err: any) {
          setState((prev) => ({
            ...prev,
            isProcessing: false,
            error: err.response?.data?.error?.message || err.message || 'Voice processing failed',
          }));
        }

        resolve();
      };

      mediaRecorder.stop();
    });
  }, [state.isRecording]);

  return {
    ...state,
    startRecording,
    stopRecording,
    cancelRecording,
    reset,
  };
}

export function getLanguageName(code: string | null): string {
  if (!code) return 'Unknown';
  return LANGUAGE_NAMES[code] || code.toUpperCase();
}
