export interface QuoteLine {
  sku: string;
  price: number;
  quantity?: number;
}

export function quoteSubtotal(lines: QuoteLine[]): number {
  return lines.reduce((total, line) => total + line.price * (line.quantity ?? 1), 0);
}

export function renderQuoteSummary(lines: QuoteLine[], taxRate = 0.13): string {
  const subtotal = quoteSubtotal(lines);
  const total = subtotal + subtotal * taxRate;
  return `Total: ${total.toFixed(2)}`;
}
