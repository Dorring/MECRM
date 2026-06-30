import { Router, Response } from 'express';
import axios from 'axios';
import { AuthenticatedRequest } from '../middleware/auth';

const router = Router();

const replayBaseUrl = process.env.REPLAY_SERVICE_URL || 'http://localhost:5011';

router.get('/:type/:id/timeline', async (req: AuthenticatedRequest, res: Response, next) => {
  try {
    const response = await axios.get(`${replayBaseUrl}/api/v1/aggregates/${req.params.type}/${req.params.id}/timeline`, {
      params: { tenant_id: req.tenantId },
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

