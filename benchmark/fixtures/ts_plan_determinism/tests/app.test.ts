import { it, expect } from 'vitest';
import { compute } from '../src/math.js';
import { assertDouble } from './helpers.js';

it('computes double using helper assertion', () => {
  assertDouble(compute(5), 5);
});

it('exercises try/catch with assertion outside', () => {
  let trapped = false;
  try {
    const fn = () => {
      throw new Error('boom');
    };
    fn();
  } catch (err) {
    trapped = true;
  }
  expect(trapped).toBe(true);
});

