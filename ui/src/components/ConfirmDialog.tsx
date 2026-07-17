/** Modal confirmation gate used before every destructive or live action. */
export function ConfirmDialog({
  open,
  message,
  onConfirm,
  onCancel,
}: {
  open: boolean
  message: string
  onConfirm: () => void
  onCancel: () => void
}) {
  if (!open) return null
  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label="Confirm action">
      <div className="card">
        <p>{message}</p>
        <div className="button-row">
          <button onClick={onCancel}>Cancel</button>
          <button className="danger" onClick={onConfirm}>
            Confirm
          </button>
        </div>
      </div>
    </div>
  )
}
