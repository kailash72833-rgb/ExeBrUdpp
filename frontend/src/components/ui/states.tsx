import { AlertTriangle, Gamepad2, RotateCw } from 'lucide-react'
import { Button } from './button'

function Bar({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-slate-700/70 ${className}`} />
}

export function AccountCardSkeleton() {
  return (
    <div className="flex flex-col gap-4 rounded-xl border border-slate-700 bg-slate-800 p-4" aria-hidden="true">
      <div className="flex gap-2">
        <Bar className="h-8 w-16 rounded-full" />
        <Bar className="h-8 w-28 rounded-full" />
        <Bar className="ml-auto h-8 w-20 rounded-full" />
      </div>
      <div className="flex items-center gap-3">
        <Bar className="size-14 rounded-xl" />
        <div className="flex flex-1 flex-col gap-2">
          <Bar className="h-4 w-1/2" />
          <Bar className="h-3 w-2/3" />
        </div>
      </div>
      <Bar className="h-24 w-full" />
      <div className="grid grid-cols-3 gap-2">
        <Bar className="h-11" />
        <Bar className="h-11" />
        <Bar className="h-11" />
      </div>
    </div>
  )
}

export function DashboardSkeleton() {
  return (
    <div className="flex flex-col gap-4" role="status" aria-label="Loading accounts">
      <span className="sr-only">Loading accounts</span>
      <AccountCardSkeleton />
      <AccountCardSkeleton />
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex flex-col items-center gap-3 rounded-xl border border-danger/40 bg-slate-800 p-6 text-center">
      <AlertTriangle className="size-8 text-danger" aria-hidden="true" />
      <p className="font-display text-sm font-bold uppercase tracking-wider text-white">Something went wrong</p>
      <p className="text-sm text-slate-400 break-words">{message}</p>
      <Button variant="outline" onClick={onRetry} icon={<RotateCw className="size-4" aria-hidden="true" />}>
        Retry
      </Button>
    </div>
  )
}

export function EmptyState() {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-slate-700 bg-slate-800/50 p-8 text-center">
      <Gamepad2 className="size-8 text-slate-500" aria-hidden="true" />
      <p className="font-display text-sm font-bold uppercase tracking-wider text-white">No accounts yet</p>
      <p className="text-sm text-slate-400">Add your first account from the classic dashboard to start leveling up.</p>
      <a
        href="/"
        className="rounded-xl border border-neon/50 bg-neon/10 px-4 py-2.5 text-sm font-bold uppercase tracking-wide text-neon transition-colors hover:bg-neon/20"
      >
        Add Account
      </a>
    </div>
  )
}
