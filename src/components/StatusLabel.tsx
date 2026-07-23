interface StatusLabelProps {
  label: string;
  tone?: "accent" | "muted" | "warning";
}

export function StatusLabel({ label, tone = "muted" }: StatusLabelProps) {
  return (
    <span className="status-label" data-tone={tone}>
      <span className="status-label__mark" aria-hidden="true" />
      {label.replace(/_/g, " ")}
    </span>
  );
}
