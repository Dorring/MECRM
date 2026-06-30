import { Response, NextFunction } from 'express';
import { AuthenticatedRequest } from './auth';
import { kafkaProducer } from '../services/kafka';
import { logger } from '../utils/logger';

export const auditMiddleware = async (
  req: AuthenticatedRequest,
  res: Response,
  next: NextFunction
): Promise<void> => {
  // Capture the original end function
  const originalEnd = res.end;
  const startTime = Date.now();
  
  // Override res.end to capture response
  res.end = function(chunk?: any, encoding?: BufferEncoding | (() => void), callback?: () => void): Response {
    // Restore original end
    res.end = originalEnd;
    
    // Calculate duration
    const duration = Date.now() - startTime;
    
    // Only audit write operations
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method)) {
      const auditEvent = {
        timestamp: new Date().toISOString(),
        tenantId: req.tenantId,
        actorType: 'user',
        actorId: req.user?.sub,
        action: `${req.method} ${req.path}`,
        resourceType: extractResourceType(req.path),
        resourceId: extractResourceId(req.path),
        requestBody: sanitizeBody(req.body),
        responseStatus: res.statusCode,
        duration,
        ipAddress: req.ip,
        userAgent: req.get('user-agent'),
        correlationId: req.headers['x-correlation-id'],
      };
      
      // Emit audit event to Kafka (fire and forget)
      kafkaProducer.send({
        topic: 'crm.audit.events',
        messages: [{
          key: req.tenantId || 'system',
          value: JSON.stringify(auditEvent),
        }],
      }).catch(error => {
        logger.error('Failed to emit audit event', { error });
      });
      
      logger.debug('Audit event emitted', {
        action: auditEvent.action,
        resourceType: auditEvent.resourceType,
        status: auditEvent.responseStatus,
      });
    }
    
    // Call original end
    return originalEnd.call(res, chunk, encoding as BufferEncoding, callback);
  };
  
  next();
};

function extractResourceType(path: string): string {
  const parts = path.split('/').filter(Boolean);
  const apiIndex = parts.findIndex(p => p === 'v1');
  return parts[apiIndex + 1] || 'unknown';
}

function extractResourceId(path: string): string | undefined {
  const parts = path.split('/').filter(Boolean);
  const apiIndex = parts.findIndex(p => p === 'v1');
  const id = parts[apiIndex + 2];
  if (id && /^[0-9a-f-]{36}$/i.test(id)) {
    return id;
  }
  return undefined;
}

function sanitizeBody(body: any): any {
  if (!body) return undefined;
  
  // Remove sensitive fields
  const sanitized = { ...body };
  const sensitiveFields = ['password', 'passwordHash', 'token', 'secret', 'apiKey'];
  
  for (const field of sensitiveFields) {
    if (field in sanitized) {
      sanitized[field] = '[REDACTED]';
    }
  }
  
  return sanitized;
}
