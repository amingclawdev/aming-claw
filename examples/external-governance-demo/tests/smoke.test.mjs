import { readFileSync } from "node:fs";
import { join } from "node:path";

const service = readFileSync(join(process.cwd(), "src", "demo_app", "service.py"), "utf8");
const widget = readFileSync(join(process.cwd(), "web", "widget.ts"), "utf8");
const legacyWidget = readFileSync(join(process.cwd(), "web", "widget.js"), "utf8");
const checkout = readFileSync(join(process.cwd(), "web", "checkout.ts"), "utf8");
const contract = readFileSync(join(process.cwd(), "contracts", "quote.schema.json"), "utf8");
const pricing = readFileSync(join(process.cwd(), "state", "pricing-rules.json"), "utf8");
const l4Doc = readFileSync(join(process.cwd(), "docs", "l4", "quote-contract.md"), "utf8");
const fixtureArtifact = readFileSync(join(process.cwd(), "artifacts", "l4-smoke-fixture.md"), "utf8");

if (!service.includes("calculate_total")) {
  throw new Error("Python service fixture is missing calculate_total");
}

if (!service.includes("quote_breakdown") || !service.includes("compliance_flags")) {
  throw new Error("Python service fixture is missing fan-out pricing helpers");
}

if (!widget.includes("renderQuoteSummary")) {
  throw new Error("TypeScript widget fixture is missing renderQuoteSummary");
}

if (!widget.includes("buildQuoteView") || !widget.includes("quoteBadges")) {
  throw new Error("TypeScript widget fixture is missing fan-in/fan-out helpers");
}

if (!legacyWidget.includes("renderQuote")) {
  throw new Error("JavaScript compatibility widget is missing renderQuote");
}

if (!checkout.includes("createCheckoutPayload") || !checkout.includes("renderCheckout")) {
  throw new Error("TypeScript checkout fixture is missing cross-module fan-in helpers");
}

if (!contract.includes('"quote.v1"') || !pricing.includes('"CA-ON"')) {
  throw new Error("L4 contract/state fixture assets are missing expected markers");
}

if (!l4Doc.includes("quote.v1") || !l4Doc.includes("src/demo_app/service.py")) {
  throw new Error("L4 quote contract doc is missing code binding markers");
}

if (!fixtureArtifact.includes("governance-hint") || !fixtureArtifact.includes('path="src/demo_app/service.py"')) {
  throw new Error("L4 fixture artifact is missing governance hints or service materialization block");
}

console.log("external governance mixed smoke ok");
