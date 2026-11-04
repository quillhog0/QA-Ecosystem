import { it, expect } from 'vitest';
import { add } from '../src/index';
it('adds correctly', () => {
  expect(add(2, 3)).toBe(5);
});
