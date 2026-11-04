import { it, expect } from 'vitest';
it.skip('skipped test left', () => {
  expect(2 + 2).toBe(4);
});
