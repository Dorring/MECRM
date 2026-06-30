import { Router, Response, NextFunction, Request } from 'express';
import axios from 'axios';
import multer from 'multer';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest } from '../middleware/errorHandler';
import { publishEvent, TOPICS } from '../services/kafka';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

const AGENTS_URL = (process.env.AGENTS_URL || 'http://localhost:5010').replace(/\/$/, '');

// Extend Request type to include file from multer
interface MulterRequest extends AuthenticatedRequest {
  file?: Express.Multer.File;
}

// Configure multer for audio upload (max 10MB, memory storage)
const upload = multer({
  storage: multer.memoryStorage(),
  limits: {
    fileSize: 10 * 1024 * 1024, // 10MB max
  },
  fileFilter: (_req: Request, file: Express.Multer.File, cb: multer.FileFilterCallback) => {
    const allowedMimes = [
      'audio/webm',
      'audio/wav',
      'audio/wave',
      'audio/mp3',
      'audio/mpeg',
      'audio/ogg',
      'audio/flac',
      'audio/m4a',
      'audio/x-m4a',
    ];
    if (allowedMimes.includes(file.mimetype)) {
      cb(null, true);
    } else {
      cb(new Error(`Unsupported audio format: ${file.mimetype}`));
    }
  },
});

interface TranscriptResponse {
  text: string;
  language: string | null;
  confidence: number;
  duration_seconds: number;
  processing_time_ms: number;
}

interface I18nQueryResponse {
  transcript?: TranscriptResponse;
  original_language: string;
  canonical_query: string;
  stt_latency_ms: number;
  detection_latency_ms: number;
  translation_latency_ms: number;
  total_latency_ms: number;
}

/**
 * POST /api/intelligence/voice
 * 
 * Accept audio file and return transcription with language detection.
 * Audio is streamed to agents service for processing.
 */
router.post(
  '/',
  upload.single('audio'),
  async (req: MulterRequest, res: Response, next: NextFunction) => {
    try {
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
      const startTime = Date.now();

      if (!req.file) {
        throw badRequest('No audio file provided');
      }

      const audioBuffer = req.file.buffer;
      const audioFormat = getAudioFormat(req.file.mimetype);

      // Emit VoiceQueryReceived event
      await publishEvent(TOPICS.INTELLIGENCE_VOICE_RECEIVED, {
        type: 'crm.intelligence.voice-received',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          audioFormat,
          audioSizeBytes: audioBuffer.length,
          userId: req.user?.sub,
        },
      });

      // Forward to agents service
      const response = await axios.post<I18nQueryResponse>(
        `${AGENTS_URL}/api/v1/intelligence/voice`,
        audioBuffer,
        {
          timeout: 30000, // 30s timeout for STT
          headers: {
            'Content-Type': req.file.mimetype,
            'X-Audio-Format': audioFormat,
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
          maxBodyLength: 10 * 1024 * 1024,
          maxContentLength: 10 * 1024 * 1024,
        }
      );

      const data = response.data;
      const durationMs = Date.now() - startTime;

      // Emit LanguageDetected event
      if (data.original_language) {
        await publishEvent(TOPICS.INTELLIGENCE_LANGUAGE_DETECTED, {
          type: 'crm.intelligence.language-detected',
          source: '/services/gateway',
          id: uuidv4(),
          tenantid: req.tenantId!,
          correlationid: correlationId,
          data: {
            language: data.original_language,
            transcript: data.transcript?.text?.substring(0, 100),
            confidence: data.transcript?.confidence,
            durationMs,
            userId: req.user?.sub,
          },
        });
      }

      res.json({
        transcript: data.transcript?.text || '',
        language: data.original_language,
        confidence: data.transcript?.confidence || 0,
        duration_seconds: data.transcript?.duration_seconds || 0,
        canonical_query: data.canonical_query,
        latency: {
          stt_ms: data.stt_latency_ms,
          detection_ms: data.detection_latency_ms,
          translation_ms: data.translation_latency_ms,
          total_ms: durationMs,
        },
      });
    } catch (error) {
      next(error);
    }
  }
);

/**
 * POST /api/intelligence/voice/query
 * 
 * Full voice query pipeline: transcribe, detect, translate, and execute query.
 */
router.post(
  '/query',
  upload.single('audio'),
  async (req: MulterRequest, res: Response, next: NextFunction) => {
    try {
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();
      const module = req.body?.module || req.query?.module;

      if (!req.file) {
        throw badRequest('No audio file provided');
      }

      const audioBuffer = req.file.buffer;
      const audioFormat = getAudioFormat(req.file.mimetype);

      // Forward to agents service for full voice query
      const response = await axios.post(
        `${AGENTS_URL}/api/v1/intelligence/voice/query`,
        audioBuffer,
        {
          timeout: 45000, // 45s for full pipeline
          headers: {
            'Content-Type': req.file.mimetype,
            'X-Audio-Format': audioFormat,
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-User-Roles': (req.user?.roles || []).join(','),
            'X-Correlation-Id': correlationId,
            'X-Client-Module': module || '',
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
          maxBodyLength: 10 * 1024 * 1024,
          maxContentLength: 10 * 1024 * 1024,
        }
      );

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

function getAudioFormat(mimetype: string): string {
  const formatMap: Record<string, string> = {
    'audio/webm': 'webm',
    'audio/wav': 'wav',
    'audio/wave': 'wav',
    'audio/mp3': 'mp3',
    'audio/mpeg': 'mp3',
    'audio/ogg': 'ogg',
    'audio/flac': 'flac',
    'audio/m4a': 'm4a',
    'audio/x-m4a': 'm4a',
  };
  return formatMap[mimetype] || 'webm';
}

export default router;
