import { normalizeRunStatus } from '../routes/governance';

describe('normalizeRunStatus', () => {
  it.each(['executed', 'completed'])('maps successful %s decisions to completed', (status) => {
    expect(normalizeRunStatus(status)).toBe('completed');
  });

  it('preserves known non-success terminal states', () => {
    expect(normalizeRunStatus('denied')).toBe('denied');
    expect(normalizeRunStatus('failed')).toBe('failed');
  });
});
