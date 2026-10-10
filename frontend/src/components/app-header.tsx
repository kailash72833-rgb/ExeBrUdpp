import { LogOut, RefreshCw } from 'lucide-react'
import { Button } from './ui/button'

type Props = {
  username: string
  timeLeft: string
  slots: string
  loading: boolean
  refreshing: boolean
  onRefresh: () => void
  onLogout: () => void
}

function StatItem({ label, value, loading }: { label: string; value: string; loading: boolean }) {
  return (
    <div className="min-w-0 border-l-2 border-blue-500 pl-3">
      <dt className="font-display text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{label}</dt>
      <dd className="mt-1 truncate text-base font-bold text-white">
        {loading ? <span className="inline-block h-4 w-14 animate-pulse rounded bg-slate-700" /> : value}
      </dd>
    </div>
  )
}

export function AppHeader({ username, timeLeft, slots, loading, refreshing, onRefresh, onLogout }: Props) {
  return (
    <header className="sticky top-0 z-20 border-b border-slate-700 bg-slate-900/95 backdrop-blur">
      <div className="mx-auto flex w-full max-w-lg flex-col gap-4 px-4 pb-4 pt-[max(1rem,env(safe-area-inset-top))]">
        <div className="flex min-w-0 items-center gap-3">
          <div
            aria-hidden="true"
            className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-blue-600 font-display text-xl font-extrabold text-white"
          >
            K
          </div>
          <h1 className="min-w-0 truncate font-display text-lg font-bold tracking-wider text-white">
            Krishna Coder LevelUp
          </h1>
        </div>

        <dl className="grid grid-cols-3 gap-3">
          <StatItem label="User" value={username} loading={loading} />
          <StatItem label="Time" value={timeLeft} loading={loading} />
          <StatItem label="Slots" value={slots} loading={loading} />
        </dl>

        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={onRefresh}
            aria-label="Refresh all accounts"
            className="w-11 shrink-0 px-0"
            icon={<RefreshCw className={`size-4 ${refreshing ? 'animate-spin' : ''}`} aria-hidden="true" />}
            disabled={refreshing}
          />
          <a
            href="/"
            className="inline-flex min-h-11 flex-1 items-center justify-center rounded-xl border border-white bg-white px-4 text-sm font-bold uppercase tracking-wide text-slate-900 transition-all duration-200 hover:bg-slate-200 active:scale-[0.97] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
          >
            Buy
          </a>
          <Button
            variant="outline"
            onClick={onLogout}
            className="flex-1"
            icon={<LogOut className="size-4" aria-hidden="true" />}
          >
            Logout
          </Button>
        </div>
      </div>
    </header>
  )
}
