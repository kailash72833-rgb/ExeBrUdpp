import { useState } from 'react'
import { Pause, Play, RefreshCw, Trash2 } from 'lucide-react'
import { api, type Account } from '../../lib/api'
import { safeText } from '../../lib/format'
import { Button } from '../ui/button'
import { ConfirmDialog } from '../ui/confirm-dialog'
import { useToast } from '../ui/toast'

type Action = 'pause' | 'refresh' | 'delete' | null

export function ActionControls({ account, onChanged }: { account: Account; onChanged: () => void }) {
  const toast = useToast()
  const [pending, setPending] = useState<Action>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const run = async (action: Exclude<Action, null>, fn: () => Promise<unknown>, success: string) => {
    if (pending) return
    setPending(action)
    try {
      await fn()
      toast(success, 'success')
      onChanged()
    } catch (err) {
      toast(err instanceof Error ? err.message : 'Action failed', 'error')
    } finally {
      setPending(null)
    }
  }

  const paused = Boolean(account.is_paused)

  return (
    <div className="grid grid-cols-3 gap-2 border-t border-slate-700 p-4">
      <Button
        variant="outline"
        loading={pending === 'pause'}
        disabled={Boolean(pending)}
        onClick={() => run('pause', () => api.pause(account.uid), paused ? 'Account resumed' : 'Account paused')}
        icon={paused ? <Play className="size-4" aria-hidden="true" /> : <Pause className="size-4" aria-hidden="true" />}
        className="px-2"
      >
        {paused ? 'Resume' : 'Pause'}
      </Button>
      <Button
        variant="neon"
        loading={pending === 'refresh'}
        disabled={Boolean(pending)}
        onClick={() => run('refresh', () => api.refresh(account.uid), 'Refreshing account')}
        icon={<RefreshCw className="size-4" aria-hidden="true" />}
        className="px-2"
      >
        Refresh
      </Button>
      <Button
        variant="danger"
        disabled={Boolean(pending)}
        onClick={() => setConfirmOpen(true)}
        icon={<Trash2 className="size-4" aria-hidden="true" />}
        className="px-2"
      >
        Delete
      </Button>

      <ConfirmDialog
        open={confirmOpen}
        title="Delete account?"
        description={`${safeText(account.nickname, account.uid, 32)} will be stopped and permanently removed. This cannot be undone.`}
        confirmLabel="Delete"
        loading={pending === 'delete'}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={() =>
          run('delete', () => api.remove(account.uid), 'Account deleted').finally(() => setConfirmOpen(false))
        }
      />
    </div>
  )
}
