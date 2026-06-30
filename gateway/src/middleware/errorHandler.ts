import { Request, Response, NextFunction } from 'express';
import { logger } from '../utils/logger';

export interface AppError extends Error {
  statusCode?: number;
  code?: string;
  details?: any;
}

export const errorHandler = (
  err: AppError,
  req: Request,
  res: Response,
  _next: NextFunction
): void => {
  const correlationId = req.headers['x-correlation-id'];
  
  // Log the error
  logger.error('Request error', {
    error: err.message,
    stack: err.stack,
    code: err.code,
    statusCode: err.statusCode,
    path: req.path,
    method: req.method,
    correlationId,
  });

  // Determine status code
  const statusCode = err.statusCode || 500;

  // Sanitize error message for production
  const message = process.env.NODE_ENV === 'production' && statusCode === 500
    ? 'Internal server error'
    : err.message;

  // Send error response
  res.status(statusCode).json({
    error: {
      code: err.code || 'INTERNAL_ERROR',
      message,
      details: process.env.NODE_ENV !== 'production' ? err.details : undefined,
      correlationId,
    },
  });
};

// Error factory functions
export const createError = (
  message: string,
  statusCode: number,
  code: string,
  details?: any
): AppError => {
  const error: AppError = new Error(message);
  error.statusCode = statusCode;
  error.code = code;
  error.details = details;
  return error;
};

export const badRequest = (message: string, details?: any): AppError =>
  createError(message, 400, 'BAD_REQUEST', details);

export const unauthorized = (message: string = 'Unauthorized'): AppError =>
  createError(message, 401, 'UNAUTHORIZED');

export const forbidden = (message: string = 'Forbidden'): AppError =>
  createError(message, 403, 'FORBIDDEN');

export const notFound = (message: string = 'Not found'): AppError =>
  createError(message, 404, 'NOT_FOUND');

export const conflict = (message: string, details?: any): AppError =>
  createError(message, 409, 'CONFLICT', details);

export const tooManyRequests = (message: string = 'Too many requests'): AppError =>
  createError(message, 429, 'TOO_MANY_REQUESTS');

export const internalError = (message: string = 'Internal server error'): AppError =>
  createError(message, 500, 'INTERNAL_ERROR');
