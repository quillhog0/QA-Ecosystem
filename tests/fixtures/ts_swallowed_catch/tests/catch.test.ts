import { it, expect } from 'vitest';
it('swallowed catch', () => {
  try {
    throw new Error('fail');
  } catch (err) {}
});
