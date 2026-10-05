// Preserve the six-second cadence while respecting the server's fixed-window budget.
// Receipt can vary with queueing: client request start times alone cannot determine reset.
export function coletaWaitMs(started, now, headers, smoke = false) {
  let deadline = started + (smoke ? 500 : 6000);
  if (!smoke) {
    const header = (name) => headers[Object.keys(headers).find((key) => key.toLowerCase() === name)];
    const remaining = header('x-ratelimit-remaining');
    const reset = Number(header('x-ratelimit-reset'));
    if (remaining === '0' && Number.isFinite(reset) && reset > 0) {
      deadline = Math.max(deadline, reset * 1000);
    }
  }
  return Math.max(0, deadline - now);
}
