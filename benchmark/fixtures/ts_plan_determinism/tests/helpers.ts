import { expect } from 'vitest';

export function assertDouble(actual: number, expected: number) {
  expect(actual).toBe(expected * 2);
}

