import { Router, Response } from 'express';
import axios from 'axios';
import { AuthenticatedRequest } from '../middleware/auth';

const router = Router();

const replayBaseUrl = process.env.REPLAY_SERVICE_URL || 'http://localhost:5011';

router.post('/start', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const response = await axios.post(
      `${replayBaseUrl}/api/v1/replay/start`,
      {
        ...req.body,
        tenant_id: req.tenantId,
      },
      {
        headers: {
          'X-Tenant-Id': req.tenantId,
          'X-Correlation-Id': req.headers['x-correlation-id'],
        },
      }
    );
    res.json(response.data);
  } catch (error) {
    next(error);
  }
});

router.get('/:jobId/status', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const response = await axios.get(`${replayBaseUrl}/api/v1/replay/${req.params.jobId}/status`, {
      headers: {
        'X-Tenant-Id': req.tenantId,
        'X-Correlation-Id': req.headers['x-correlation-id'],
      },
    });
    res.json(response.data);
  } catch (error) {
    next(error);
  }
});

router.get('/:jobId/diff', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const response = await axios.get(`${replayBaseUrl}/api/v1/replay/${req.params.jobId}/diff`, {
      params: req.query,
      headers: {
        'X-Tenant-Id': req.tenantId,
        'X-Correlation-Id': req.headers['x-correlation-id'],
      },
    });
    res.json(response.data);
  } catch (error) {
    next(error);
  }
});

export default router;

