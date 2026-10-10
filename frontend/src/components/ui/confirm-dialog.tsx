import { useEffect, useRef } from 'react'
import { Button } from './button'

type Props = {
  open: boolean
  title: string
  description: string
  confirmLabel: string
  loading?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({ open, title, description, confirmLabel, loading, onConfirm, onCancel }: Props) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault()
        if (!loading) onCancel()
      }}
      className="m-auto w-[calc(100%-2rem)] max-w-sm rounded-xl border border-slate-700 bg-slate-800 p-0 text-white backdrop:bg-slate-950/80"
      aria-labelledby="confirm-title"
    >
      <div className="flex flex-col gap-4 p-5">
        <h2 id="confirm-title" className="font-display text-base font-bold uppercase tracking-wider">
          {title}
        </h2>
        <p className="text-sm leading-relaxed text-slate-400 break-words">{description}</p>
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" onClick={onCancel} disabled={loading}>
            Cancel
          </Button>
          <Button variant="danger" onClick={onConfirm} loading={loading}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </dialog>
  )
}
