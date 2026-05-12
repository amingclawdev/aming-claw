export function formatSemanticValue(value: unknown, maxLength = 360): string {
  const text = semanticValueText(value);
  if (!text) return "";
  return text.length > maxLength ? `${text.slice(0, Math.max(0, maxLength - 3))}...` : text;
}

function semanticValueText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value
      .map((item) => semanticValueText(item))
      .filter(Boolean)
      .join("; ");
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const preferred = [
      "summary",
      "description",
      "label",
      "direction",
      "directionality",
      "risk",
      "level",
      "severity",
      "basis",
      "reason",
    ];
    const preferredParts = preferred
      .map((key) => {
        const text = semanticValueText(record[key]);
        return text ? `${key}: ${text}` : "";
      })
      .filter(Boolean);
    if (preferredParts.length) return preferredParts.join("; ");
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return "";
}
