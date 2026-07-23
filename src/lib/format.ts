export function formatDate(value: string | undefined): string {
  if (!value) return "Date unavailable";
  const date = new Date(`${value.slice(0, 10)}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC"
  }).format(date);
}

export function formatTimestamp(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Moscow",
    timeZoneName: "short"
  }).format(date);
}

export function asText(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value.join("; ");
  return value;
}

export function confidenceLabel(value: unknown): string {
  if (typeof value === "number") {
    if (value >= 0.9) return `Very high · ${Math.round(value * 100)}%`;
    if (value >= 0.7) return `High · ${Math.round(value * 100)}%`;
    if (value >= 0.4) return `Moderate · ${Math.round(value * 100)}%`;
    return `Low · ${Math.round(value * 100)}%`;
  }
  if (typeof value === "string") return value.replace(/_/g, " ");
  if (value && typeof value === "object") {
    const candidate = value as {
      label?: string;
      level?: string;
      value?: number;
      score?: number;
    };
    const label = candidate.label ?? candidate.level;
    const score = candidate.value ?? candidate.score;
    if (label && score != null) {
      return `${label.replace(/_/g, " ")} · ${Math.round(score * 100)}%`;
    }
    return label?.replace(/_/g, " ") ?? confidenceLabel(score);
  }
  return "Not assigned";
}

export function formatLocator(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "Locator unavailable";

  const locator = value as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof locator.path === "string") parts.push(locator.path);
  if (typeof locator.page === "number" || typeof locator.page === "string") {
    parts.push(`p. ${String(locator.page)}`);
  }
  if (typeof locator.section === "string") parts.push(locator.section);
  if (typeof locator.theorem === "string") parts.push(locator.theorem);
  if (typeof locator.equation === "string") parts.push(`eq. ${locator.equation}`);
  if (typeof locator.cell === "string") parts.push(`cell ${locator.cell}`);
  if (typeof locator.csv_row === "string" || typeof locator.csv_row === "number") {
    parts.push(`rows ${String(locator.csv_row)}`);
  }
  if (typeof locator.json_pointer === "string") parts.push(locator.json_pointer);

  const start = locator.line_start;
  const end = locator.line_end;
  if (typeof start === "number" || typeof start === "string") {
    parts.push(`lines ${String(start)}${end != null ? `–${String(end)}` : ""}`);
  }

  return parts.length > 0 ? parts.join(" · ") : JSON.stringify(locator);
}
