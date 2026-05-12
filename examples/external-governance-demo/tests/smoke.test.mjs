import { readFileSync } from "node:fs";
import { join } from "node:path";

const service = readFileSync(join(process.cwd(), "src", "demo_app", "service.py"), "utf8");
const widget = readFileSync(join(process.cwd(), "web", "widget.ts"), "utf8");

if (!service.includes("calculate_total")) {
  throw new Error("Python service fixture is missing calculate_total");
}

if (!widget.includes("renderQuoteSummary")) {
  throw new Error("TypeScript widget fixture is missing renderQuoteSummary");
}

console.log("external governance mixed smoke ok");
