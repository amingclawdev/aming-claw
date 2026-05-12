import {
  buildQuoteView,
  renderAuditBadge,
  renderQuoteCard,
  type QuoteLine,
  type QuoteView,
} from "./widget";

export interface CheckoutPayload {
  contract: "quote.v1";
  quote: QuoteView;
  reviewLabel: string;
}

export function createCheckoutPayload(lines: QuoteLine[], tier = "standard"): CheckoutPayload {
  return {
    contract: "quote.v1",
    quote: buildQuoteView(lines, tier),
    reviewLabel: renderAuditBadge(lines),
  };
}

export function createCheckoutSummary(lines: QuoteLine[], tier = "standard"): string {
  const payload = createCheckoutPayload(lines, tier);
  return `${payload.contract} ${payload.quote.total.toFixed(2)} ${payload.reviewLabel}`;
}

export function renderCheckout(lines: QuoteLine[], tier = "standard"): string {
  return `${renderQuoteCard(lines, tier)} | ${createCheckoutSummary(lines, tier)}`;
}
