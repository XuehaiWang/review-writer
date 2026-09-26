export function displayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(displayText).filter(Boolean).join(", ");
  if (typeof value === "object" && "value" in value) return displayText((value as { value: unknown }).value);
  return JSON.stringify(value);
}
