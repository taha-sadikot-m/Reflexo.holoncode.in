// House style on screen: a long em-dash reads as a comma. Applied to everything the API returns, so text stored
// before the style (old messages, notifications, change summaries) shows the same way.
export function plain(text: string): string {
  if (!text.includes("\u2014")) return text;
  return text
    .split("\n")
    .map((line) =>
      line
        .replace(/[ \t]*\u2014[ \t]*/g, ", ")
        .replace(/^\s*,\s*/, "")
        .replace(/,\s*([,.!?:;])/g, "$1")
        .replace(/,\s*$/, ""),
    )
    .join("\n");
}

export function plainDeep<T>(value: T): T {
  if (typeof value === "string") return plain(value) as T;
  if (Array.isArray(value)) return value.map(plainDeep) as T;
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, plainDeep(v)])) as T;
  }
  return value;
}
