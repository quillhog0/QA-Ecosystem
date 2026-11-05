import { it, expect } from "vitest";
import { calculateTotal } from "../src/index.js";

it("calculates sum of items", () => {
  expect(calculateTotal([10, 20, 30])).toBe(60);
});

it("handles empty list correctly", () => {
  expect(calculateTotal([])).toBe(0);
});

