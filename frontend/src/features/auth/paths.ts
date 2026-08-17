export function safeReturnPath(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "/workspace";
  if (value === "/" || value.startsWith("/login")) return "/workspace";
  return value;
}
