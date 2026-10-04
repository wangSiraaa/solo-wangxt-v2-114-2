const BASE = "/api";

async function get(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path}: ${res.status}`);
  return data;
}

export const api = {
  plots: () => get("/plots/"),
  measurements: (campaign) =>
    get(`/measurements/?campaign=${encodeURIComponent(campaign)}`),
  campaigns: () => get("/campaigns/"),
  equations: () => get("/equations/"),
  conflicts: (status, interval) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (interval) q.set("interval", interval);
    const s = q.toString();
    return get(`/conflicts/${s ? `?${s}` : ""}`);
  },
  resolveConflict: (id, payload) =>
    post(`/conflicts/${id}/resolve/`, payload),
  estimates: (intervalId) =>
    get(`/estimates/${intervalId ? `?interval=${intervalId}` : ""}`),
  estimate: (id) => get(`/estimates/${id}/`),
  createEstimate: (payload) => post("/estimates/", payload),
  confirmEstimate: (id) => post(`/estimates/${id}/confirm/`),

  // multi-campaign chains
  sequences: () => get("/sequences/"),
  createSequence: (payload) => post("/sequences/", payload),
  addCampaigns: (id, campaigns) =>
    post(`/sequences/${id}/add_campaigns/`, { campaigns }),
  intervals: (sequence) =>
    get(`/intervals/${sequence ? `?sequence=${sequence}` : ""}`),
  interval: (id) => get(`/intervals/${id}/`),
  refreshInterval: (id) => post(`/intervals/${id}/refresh/`),
  intervalProvenance: (id) => get(`/intervals/${id}/provenance/`),
  runIntervalEstimate: (id, payload) =>
    post(`/intervals/${id}/estimates/`, payload),
  plotTimeline: (sequence, plot) =>
    get(`/timeline/?sequence=${encodeURIComponent(sequence)}`
        + `&plot=${encodeURIComponent(plot)}`),
  treeTimeline: (sequence, treeId) =>
    get(`/timeline/?sequence=${encodeURIComponent(sequence)}`
        + `&tree=${treeId}`),
};
