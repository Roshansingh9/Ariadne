// Formats a price for display.
export function formatPrice(p) {
  return round(p);
}

function round(x) {
  return Math.round(x * 100) / 100;
}
