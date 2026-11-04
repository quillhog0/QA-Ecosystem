import { it, expect } from 'vitest';
import { token, loadRows } from '../src/index';
it('reads token from env', () => {
  process.env.BOT_TOKEN = 'abc';
  expect(token()).toBe('abc');
});
it('loads rows', () => {
  expect(loadRows(new URL('./fixtures/rows.json', import.meta.url).pathname)[0].amount).toBe(1);
});
