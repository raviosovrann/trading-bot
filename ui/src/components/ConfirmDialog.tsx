import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Modal confirmation gate used before every destructive or live action.
 *
 * This is the last thing between an operator and a real order, so it carries
 * the guarantees a modal actually needs (#132):
 *
 * - **Focus enters and stays.** Tab and Shift+Tab wrap inside the dialog, and
 *   focus returns to whatever opened it on close — losing your place is what
 *   makes a modal unusable by keyboard.
 * - **The default is the safe action.** Cancel takes focus, not Confirm, so a
 *   reflex Enter does nothing.
 * - **The background is inert.** The dialog renders in a portal and every other
 *   top-level element is marked `inert` and `aria-hidden`, so neither a pointer
 *   nor a screen reader can reach the controls behind it. `aria-hidden` alone
 *   would leave them clickable and tabbable.
 * - **Confirm fires at most once.** It is disabled for the whole in-flight
 *   request — the same guard #126 needs for lifecycle actions — and a failed
 *   request re-enables it rather than stranding the operator on a dead dialog.
 */
export function ConfirmDialog({
  open,
  title = 'Confirm action',
  message,
  onConfirm,
  onCancel,
}: {
  open: boolean
  /** Short action name; becomes the dialog's accessible name. */
  title?: string
  /** What will happen, including any LIVE risk; the accessible description. */
  message: string
  onConfirm: () => void | Promise<unknown>
  onCancel: () => void
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const restoreTo = useRef<HTMLElement | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const titleId = useId()
  const messageId = useId()

  // Remember the trigger before focus moves, and restore it on close.
  useEffect(() => {
    if (!open) return
    restoreTo.current = document.activeElement as HTMLElement | null
    setError(null)
    return () => {
      restoreTo.current?.focus?.()
    }
  }, [open])

  // Land on Cancel: for a destructive action the safe option is the one a
  // keyboard user should reach first.
  useEffect(() => {
    if (!open) return
    dialogRef.current?.querySelector<HTMLElement>('[data-autofocus]')?.focus()
  }, [open])

  // Make everything outside the dialog unreachable while it is open.
  useEffect(() => {
    if (!open) return
    const dialog = dialogRef.current
    const siblings = Array.from(document.body.children).filter((node) => node !== dialog)
    const previous = siblings.map((node) => ({
      node,
      inert: node.hasAttribute('inert'),
      hidden: node.getAttribute('aria-hidden'),
    }))
    siblings.forEach((node) => {
      node.setAttribute('inert', '')
      node.setAttribute('aria-hidden', 'true')
    })
    return () => {
      previous.forEach(({ node, inert, hidden }) => {
        if (!inert) node.removeAttribute('inert')
        if (hidden === null) node.removeAttribute('aria-hidden')
        else node.setAttribute('aria-hidden', hidden)
      })
    }
  }, [open])

  const confirm = useCallback(async () => {
    if (pending) return
    setPending(true)
    setError(null)
    try {
      await onConfirm()
    } catch (err) {
      // Report it here rather than on the page behind: the background is
      // inert while the dialog is open, so an error rendered out there would
      // be unreachable to both pointer and screen reader.
      setError(String(err))
    } finally {
      setPending(false)
    }
  }, [onConfirm, pending])

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      // Dismissing mid-flight would hide an action that is still running.
      if (event.key === 'Escape' && !pending) {
        event.preventDefault()
        onCancel()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
      )
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    },
    [onCancel, pending],
  )

  if (!open) return null

  return createPortal(
    <div
      className="overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={messageId}
      ref={dialogRef}
      onKeyDown={onKeyDown}
    >
      <div className="card">
        <h2 id={titleId}>{title}</h2>
        <p id={messageId}>{message}</p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="button-row">
          <button data-autofocus disabled={pending} onClick={onCancel}>
            Cancel
          </button>
          <button className="danger" disabled={pending} onClick={() => void confirm()}>
            {pending ? 'Working…' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
