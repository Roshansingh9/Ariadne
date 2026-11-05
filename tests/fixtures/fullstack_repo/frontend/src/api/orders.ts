export async function createOrder(items: unknown[]) {
  const res = await fetch("/api/orders", { method: "POST", body: JSON.stringify({ items }) });
  return res.json();
}

export function removeOrder(id: number) {
  return fetch(`/api/orders/${id}`, { method: "DELETE" });
}

export function loadReport() {
  return fetch("https://analytics.example.invalid/report");
}
