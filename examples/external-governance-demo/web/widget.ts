export interface QuoteLine {
  sku: string;
  price: number;
  quantity?: number;
  hazmat?: boolean;
}

export interface QuoteView {
  subtotal: number;
  discount: number;
  tax: number;
  total: number;
  badges: string[];
}

export function normalizeLine(line: QuoteLine): Required<QuoteLine> {
  return {
    sku: line.sku || "unknown",
    price: line.price,
    quantity: line.quantity ?? 1,
    hazmat: line.hazmat ?? false,
  };
}

export function lineTotal(line: QuoteLine): number {
  const normalized = normalizeLine(line);
  return normalized.price * normalized.quantity;
}

export function quoteSubtotal(lines: QuoteLine[]): number {
  return lines.reduce((total, line) => total + lineTotal(line), 0);
}

export function quoteDiscount(subtotal: number, tier = "standard"): number {
  if (tier === "enterprise") return subtotal * 0.15;
  if (tier === "member") return subtotal * 0.05;
  return 0;
}

export function quoteTax(amount: number, taxRate = 0.13): number {
  return amount * taxRate;
}

export function quoteBadges(lines: QuoteLine[]): string[] {
  const normalized = lines.map(normalizeLine);
  return normalized.some((line) => line.hazmat) ? ["hazmat"] : [];
}

export function buildQuoteView(lines: QuoteLine[], tier = "standard", taxRate = 0.13): QuoteView {
  const subtotal = quoteSubtotal(lines);
  const discount = quoteDiscount(subtotal, tier);
  const taxable = subtotal - discount;
  const tax = quoteTax(taxable, taxRate);
  return {
    subtotal,
    discount,
    tax,
    total: taxable + tax,
    badges: quoteBadges(lines),
  };
}

export function renderQuoteSummary(lines: QuoteLine[], taxRate = 0.13): string {
  const subtotal = quoteSubtotal(lines);
  const total = subtotal + quoteTax(subtotal, taxRate);
  return `Total: ${total.toFixed(2)}`;
}

export function renderQuoteCard(lines: QuoteLine[], tier = "standard"): string {
  const view = buildQuoteView(lines, tier);
  return `${renderQuoteSummary(lines)} / ${view.badges.length} badges`;
}

export function renderAuditBadge(lines: QuoteLine[]): string {
  const badges = quoteBadges(lines);
  return badges.length ? `Review: ${badges.join(",")}` : "Review: clear";
}
