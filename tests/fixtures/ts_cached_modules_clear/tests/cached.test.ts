import { it, expect } from 'vitest';

it('passes under sandbox using cached node_modules', () => {
  expect(1 + 1).toBe(2);
});
