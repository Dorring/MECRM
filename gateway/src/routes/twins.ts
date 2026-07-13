import { Router, Response, NextFunction } from 'express';
import axios from 'axios';
import { body, param, validationResult } from 'express-validator';
import { AuthenticatedRequest } from '../middleware/auth';
import { badRequest, forbidden } from '../middleware/errorHandler';
import { publishEvent, TOPICS } from '../services/kafka';
import { uuidv4 } from '../utils/uuid';

const router = Router();

const AGENTS_URL = (process.env.AGENTS_URL || 'http://localhost:5010').replace(/\/$/, '');

// Allowed scenarios
const ALLOWED_SCENARIOS = [
  'price_increase_5',
  'price_increase_10',
  'price_increase_20',
  'feature_removal',
  'contract_renewal',
  'upsell_small',
  'upsell_large',
];

// Roles allowed to view twins
const TWIN_VIEW_ROLES = ['admin', 'super_admin', 'sales_lead', 'sales'];
// Roles allowed to run simulations
const TWIN_SIMULATE_ROLES = ['admin', 'super_admin', 'sales_lead'];

/**
 * Check if user has required role
 */
const hasRole = (userRoles: string[], allowedRoles: string[]): boolean => {
  return userRoles.some(role => allowedRoles.includes(role));
};

/**
 * GET /api/intelligence/twin/:customerId
 * Get the twin profile for a customer
 */
router.get(
  '/:customerId',
  param('customerId').isUUID(),
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, TWIN_VIEW_ROLES)) {
        throw forbidden('Insufficient permissions to view customer twins');
      }

      const customerId = req.params.customerId;
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.get(
        `${AGENTS_URL}/api/v1/intelligence/twins/${customerId}`,
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * POST /api/intelligence/twin/simulate
 * Run a simulation scenario for a customer
 */
router.post(
  '/simulate',
  body('customer_id').isUUID(),
  body('scenario').isString().isIn(ALLOWED_SCENARIOS),
  body('params').optional().isObject(),
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, TWIN_SIMULATE_ROLES)) {
        throw forbidden('Insufficient permissions to run simulations');
      }

      const { customer_id, scenario, params } = req.body;
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.post(
        `${AGENTS_URL}/api/v1/intelligence/twins/simulate`,
        {
          tenant_id: req.tenantId,
          customer_id,
          scenario,
          user_id: req.user?.sub,
          params: params || {},
        },
        {
          timeout: 10000,
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      // Publish simulation event
      await publishEvent(TOPICS.TWIN_SIMULATION_EXECUTED, {
        type: 'crm.intelligence.twin-simulation-executed',
        source: '/services/gateway',
        id: uuidv4(),
        tenantid: req.tenantId!,
        correlationid: correlationId,
        data: {
          customerId: customer_id,
          scenario,
          userId: req.user?.sub,
          confidence: response.data?.confidence,
          outcomes: response.data?.outcomes,
        },
      });

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * GET /api/intelligence/twin/:customerId/history
 * Get simulation history for a customer
 */
router.get(
  '/:customerId/history',
  param('customerId').isUUID(),
  async (req: AuthenticatedRequest, res: Response, next: NextFunction) => {
    try {
      const errors = validationResult(req);
      if (!errors.isEmpty()) {
        throw badRequest('Validation failed', errors.array());
      }

      const userRoles = req.user?.roles || [];
      if (!hasRole(userRoles, TWIN_VIEW_ROLES)) {
        throw forbidden('Insufficient permissions to view simulation history');
      }

      const customerId = req.params.customerId;
      const correlationId = (req.headers['x-correlation-id'] as string) || uuidv4();

      const response = await axios.get(
        `${AGENTS_URL}/api/v1/intelligence/twins/${customerId}/history`,
        {
          timeout: 5000,
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Id': req.tenantId,
            'X-User-Id': req.user?.sub,
            'X-Correlation-Id': correlationId,
            ...(req.headers.authorization ? { Authorization: String(req.headers.authorization) } : {}),
          },
        }
      );

      res.json(response.data);
    } catch (error) {
      next(error);
    }
  }
);

/**
 * GET /api/intelligence/twin/scenarios
 * Get available simulation scenarios
 */
router.get(
  '/scenarios',
  async (req: AuthenticatedRequest, res: Response) => {
    res.json({
      scenarios: [
        { id: 'price_increase_5', label: 'Price Increase 5%', category: 'pricing' },
        { id: 'price_increase_10', label: 'Price Increase 10%', category: 'pricing' },
        { id: 'price_increase_20', label: 'Price Increase 20%', category: 'pricing' },
        { id: 'feature_removal', label: 'Feature Removal', category: 'product' },
        { id: 'contract_renewal', label: 'Contract Renewal', category: 'retention' },
        { id: 'upsell_small', label: 'Upsell (Small)', category: 'growth' },
        { id: 'upsell_large', label: 'Upsell (Large)', category: 'growth' },
      ],
    });
  }
);

export default router;
