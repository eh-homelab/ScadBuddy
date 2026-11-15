/**
 * The order page's pay link sets this flag, and the pay page reads and clears it, so the
 * pay page knows the payer has an order page to go back to. The link keeps
 * `rel="noreferrer"` (a provider's hosted page never learns `/o?order=<id>`), so the
 * referrer cannot tell. Storage may be blocked: then there is no flag.
 */
const KEY = "sb_pay_from_order";

/** Mark a click on a pay link that stays on this site. */
export function markPayFromOrder(payUrl: string, event?: MouseEvent): void {
  if (event && (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
  try {
    if (
      new URL(payUrl, window.location.href).origin === window.location.origin
    ) {
      window.sessionStorage.setItem(KEY, "1");
    }
  } catch {
    // No storage, or a link that is no URL: the pay page offers no way back.
  }
}

export function payCameFromOrder(): boolean {
  try {
    return window.sessionStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function clearPayFromOrder(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // Nothing to clear.
  }
}
