const { mul } = require('../src/calc');
test('multiplies correctly', () => {
  expect(mul(3, 4)).toBe(12);
});
