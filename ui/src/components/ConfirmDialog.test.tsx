import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { ConfirmDialog } from './ConfirmDialog'

/** A trigger plus the dialog, so focus restore has somewhere real to return to. */
function Harness({
  onConfirm = vi.fn(),
  message = 'Stop BTC/USD?',
  title = 'Stop bot',
}: {
  onConfirm?: () => void | Promise<void>
  message?: string
  title?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button onClick={() => setOpen(true)}>Open</button>
      <button>Behind</button>
      <ConfirmDialog
        open={open}
        title={title}
        message={message}
        onConfirm={onConfirm}
        onCancel={() => setOpen(false)}
      />
    </div>
  )
}

describe('ConfirmDialog accessibility (#132)', () => {
  it('moves focus into the dialog, defaulting to the safe action', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    // Cancel, not Confirm: for a destructive action the default must be the
    // one that does nothing.
    await waitFor(() => expect(screen.getByRole('button', { name: /cancel/i })).toHaveFocus())
  })

  it('traps Tab inside the dialog', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    const dialog = screen.getByRole('dialog')

    for (let i = 0; i < 6; i++) {
      await userEvent.tab()
      expect(dialog).toContainElement(document.activeElement as HTMLElement)
    }
  })

  it('traps Shift+Tab inside the dialog too', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    const dialog = screen.getByRole('dialog')

    for (let i = 0; i < 4; i++) {
      await userEvent.tab({ shift: true })
      expect(dialog).toContainElement(document.activeElement as HTMLElement)
    }
  })

  it('returns focus to the trigger when closed', async () => {
    render(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Open' })
    await userEvent.click(trigger)

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))

    // Losing your place is the thing that makes modals unusable by keyboard.
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('cancels on Escape', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    await userEvent.keyboard('{Escape}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('makes the background inert while open', async () => {
    const { container } = render(<Harness />)
    const behind = screen.getByRole('button', { name: 'Behind' })
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    // Unreachable to assistive technology: the accessibility tree no longer
    // exposes it at all.
    expect(screen.queryByRole('button', { name: 'Behind' })).toBeNull()
    // ...and unreachable to pointer and keyboard, which aria-hidden alone
    // would not achieve.
    // Testing Library renders into a div appended to <body>, which is exactly
    // the kind of top-level sibling the dialog marks.
    expect(container).toHaveAttribute('inert')
    expect(behind.closest('[inert]')).toBe(container)
  })

  it('restores the background when the dialog closes', async () => {
    const { container } = render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))

    // Leaving the app inert would lock the operator out entirely.
    await waitFor(() => expect(container).not.toHaveAttribute('inert'))
    expect(screen.getByRole('button', { name: 'Behind' })).toBeInTheDocument()
  })

  it('describes the action to a screen reader', async () => {
    render(<Harness title="Enable LIVE trading" message="Real orders will be sent to coinbase." />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAccessibleName('Enable LIVE trading')
    expect(dialog).toHaveAccessibleDescription(/Real orders will be sent to coinbase/)
  })
})

describe('ConfirmDialog in-flight guard (#132, shared with #126)', () => {
  it('fires at most one request however many times confirm is clicked', async () => {
    let release: () => void = () => {}
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    )
    render(<Harness onConfirm={onConfirm} />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    const confirm = screen.getByRole('button', { name: /confirm/i })
    await userEvent.click(confirm)
    await userEvent.click(confirm)
    await userEvent.click(confirm)

    // Double-submitting a start/stop is exactly what #126 serialized on the
    // server; the client must not rely on that alone.
    expect(onConfirm).toHaveBeenCalledTimes(1)
    release()
  })

  it('shows progress and disables both actions while pending', async () => {
    let release: () => void = () => {}
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    )
    render(<Harness onConfirm={onConfirm} />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /working/i })).toBeDisabled()
      expect(screen.getByRole('button', { name: /cancel/i })).toBeDisabled()
    })

    release()
  })

  it('re-enables confirm when the request fails, so it can be retried', async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error('409 conflict'))
    render(<Harness onConfirm={onConfirm} />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))

    // A failed action must not leave the operator stuck with a dead dialog.
    await waitFor(() => expect(screen.getByRole('button', { name: /confirm/i })).toBeEnabled())
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('ignores Escape while a request is in flight', async () => {
    let release: () => void = () => {}
    const onConfirm = vi.fn(() => new Promise<void>((resolve) => (release = resolve)))
    render(<Harness onConfirm={onConfirm} />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))

    await userEvent.keyboard('{Escape}')

    // Dismissing mid-flight would hide an action the operator still has running.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    release()
  })

  it('supports a synchronous onConfirm', async () => {
    const onConfirm = vi.fn()
    render(<Harness onConfirm={onConfirm} />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))

    expect(onConfirm).toHaveBeenCalledTimes(1)
  })
})
