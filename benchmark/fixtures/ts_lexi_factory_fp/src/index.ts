import { readFileSync } from "node:fs";

export function token(): string {
  return process.env.BOT_TOKEN ?? "";
}

export function loadRows(path: string): Array<Record<string, unknown>> {
  return JSON.parse(readFileSync(path, "utf-8"));
}
