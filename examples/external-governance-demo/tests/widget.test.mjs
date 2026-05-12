import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(join(process.cwd(), "web", "widget.ts"), "utf8");
const checkout = readFileSync(join(process.cwd(), "web", "checkout.ts"), "utf8");

for (const symbol of [
  "normalizeLine",
  "lineTotal",
  "quoteSubtotal",
  "quoteDiscount",
  "quoteTax",
  "quoteBadges",
  "buildQuoteView",
  "renderQuoteCard",
  "renderAuditBadge",
]) {
  if (!source.includes(`function ${symbol}`)) {
    throw new Error(`widget.ts is missing ${symbol}`);
  }
}

for (const symbol of ["createCheckoutPayload", "createCheckoutSummary", "renderCheckout"]) {
  if (!checkout.includes(`function ${symbol}`)) {
    throw new Error(`checkout.ts is missing ${symbol}`);
  }
}

if (!checkout.includes('from "./widget"')) {
  throw new Error("checkout.ts should import widget helpers for graph relation smoke");
}

console.log("external governance widget smoke ok");
