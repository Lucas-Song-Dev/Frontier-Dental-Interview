export function Icon({ name }) {
  const common = {
    width: 16,
    height: 16,
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.2,
    "aria-hidden": true,
  };
  if (name === "files") {
    return (
      <svg {...common}>
        <path d="M3 2.5h6l3 3V13.5H3z" />
        <path d="M9 2.5V6h3" />
      </svg>
    );
  }
  if (name === "comment") {
    return (
      <svg {...common}>
        <path d="M3 3.5h10v7H6.5L3 13z" />
      </svg>
    );
  }
  if (name === "history") {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.2" />
        <path d="M8 5.2V8l2 1.4" />
      </svg>
    );
  }
  if (name === "gear") {
    return (
      <svg {...common} viewBox="0 0 24 24" strokeWidth="1.6">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    );
  }
  if (name === "left") {
    return (
      <svg {...common}>
        <rect x="2" y="2.5" width="12" height="11" rx="1" />
        <path d="M6.2 2.5v11" />
      </svg>
    );
  }
  if (name === "bottom") {
    return (
      <svg {...common}>
        <rect x="2" y="2.5" width="12" height="11" rx="1" />
        <path d="M2 9.8h12" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <rect x="2" y="2.5" width="12" height="11" rx="1" />
      <path d="M9.8 2.5v11" />
    </svg>
  );
}
