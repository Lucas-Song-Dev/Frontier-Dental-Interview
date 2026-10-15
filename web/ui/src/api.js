export async function getJSON(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${response.status} ${path}`);
  }
  return response.json();
}

export function getCatalog() {
  return getJSON("/api/catalog");
}

export function getRows() {
  return getJSON("/api/rows");
}

export function getProduct(slug) {
  return getJSON(`/api/product/${slug}`);
}

export function getQueries() {
  return getJSON("/api/queries");
}

export function getJobs() {
  return getJSON("/api/jobs");
}

export function getSettings() {
  return getJSON("/api/settings");
}

export function getPipeline() {
  return getJSON("/api/pipeline");
}

export async function saveSettings(settings) {
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Could not save settings.");
  }
  return payload;
}

export async function refreshSnapshot() {
  const response = await fetch("/api/refresh", { method: "POST" });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Refresh failed.");
  }
  return payload;
}

export async function ask(body) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response.json();
}
