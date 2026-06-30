import { Response, NextFunction } from 'express';
import { AuthenticatedRequest } from './auth';
import { forbidden, badRequest } from './errorHandler';
import { logger } from '../utils/logger';

export const tenantMiddleware = async (
  req: AuthenticatedRequest,
  res: Response,
  next: NextFunction
): Promise<void> => {
  try {
    // Get tenant from token (already set by auth middleware)
    const tokenTenantId = req.user?.tenantId;
    
    // Get tenant from header (optional override for super admins)
    const headerTenantId = req.headers['x-tenant-id'] as string;
    
    if (!tokenTenantId) {
      throw badRequest('Tenant ID not found in token');
    }
    
    // If header tenant is different, user must be super admin
    if (headerTenantId && headerTenantId !== tokenTenantId) {
      if (!req.user?.roles.includes('super_admin')) {
        throw forbidden('Cannot access other tenant resources');
      }
      
      logger.info('Super admin cross-tenant access', {
        userId: req.user?.sub,
        fromTenant: tokenTenantId,
        toTenant: headerTenantId,
      });
      
      req.tenantId = headerTenantId;
    } else {
      req.tenantId = tokenTenantId;
    }
    
    // Set tenant context header for downstream services
    req.headers['x-tenant-id'] = req.tenantId;
    req.headers['x-token-tenant-id'] = tokenTenantId;
    
    logger.debug('Tenant context set', { tenantId: req.tenantId });
    
    next();
  } catch (error) {
    next(error);
  }
};

// Validate that a resource belongs to the current tenant
export const validateTenantResource = (
  resourceTenantId: string,
  requestTenantId: string
): boolean => {
  return resourceTenantId === requestTenantId;
};
