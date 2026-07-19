import type { Trade, TradeKind } from './types'

/**
 * How one order-lifecycle event should read in the history table (#135).
 *
 * The point of this mapping is that a submission and a fill no longer look
 * alike. Before #135 every outcome was rendered as a "trade" row, so an order
 * the venue had merely acknowledged -- or one that never left the process at
 * all, in dry-run mode -- was presented to the operator as a completed trade.
 */
export interface TradeEventDescription {
  /** Short label for the event, e.g. "Filled". */
  label: string
  /** Quantity and price detail, or null when the event carries none. */
  detail: string | null
  /**
   * Severity class for styling. `neutral` means nothing traded and nothing
   * went wrong -- the state most easily mistaken for a completed trade.
   */
  tone: 'positive' | 'neutral' | 'negative'
}

const LABELS: Record<TradeKind, string> = {
  submitted: 'Submitted',
  order_status: 'Filled',
  dry_run: 'Dry run',
  rejected: 'Rejected',
  canceled: 'Canceled',
}

function formatQty(qty: number | null, price: number | null): string | null {
  if (qty === null || !Number.isFinite(qty)) return null
  const base = String(qty)
  if (price === null || !Number.isFinite(price) || price <= 0) return base
  return `${base} @ ${price}`
}

/**
 * Describe a stored lifecycle event for display.
 *
 * Legacy rows written before #135 carry no `kind`. They are shown using the
 * flat fields they do have rather than being reinterpreted, because the
 * information needed to classify them honestly was never recorded.
 */
export function describeTrade(trade: Trade): TradeEventDescription {
  const kind = trade.kind

  if (kind === null) {
    return {
      label: trade.status || trade.action || 'Event',
      detail: null,
      tone: trade.ok ? 'neutral' : 'negative',
    }
  }

  if (kind === 'order_status') {
    return {
      label: LABELS.order_status,
      detail: formatQty(trade.filled_qty, trade.avg_price),
      tone: 'positive',
    }
  }

  if (kind === 'rejected') {
    return {
      label: LABELS.rejected,
      detail: trade.reason,
      tone: 'negative',
    }
  }

  return {
    label: LABELS[kind] ?? 'Event',
    detail: formatQty(trade.qty, trade.avg_price),
    // A submission, a cancel and a dry run all moved no quantity. Rendering
    // them as anything other than neutral is what created the original
    // misreading.
    tone: 'neutral',
  }
}
