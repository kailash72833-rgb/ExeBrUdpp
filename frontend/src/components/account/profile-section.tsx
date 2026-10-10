import { Copy } from 'lucide-react'
import { formatNumber, initialOf, safeText, timeAgo } from '../../lib/format'
import type { Account } from '../../lib/api'
import { useToast } from '../ui/toast'

export function ProfileSection({ account }: { account: Account }) {
  const toast = useToast()
  const nickname = safeText(account.nickname, 'Unknown Player', 32)
  const uid = safeText(account.uid, '—', 24)

  const copyUid = async () => {
    try {
      await navigator.clipboard.writeText(uid)
      toast('UID copied', 'success')
    } catch {
      toast('Copy failed', 'error')
    }
  }

  const details = [
    { label: 'Created', value: timeAgo(account.added_at) },
    { label: 'Played', value: formatNumber(account.matches_played) },
    { label: 'Nickname', value: nickname },
    { label: 'Region', value: safeText(account.region, 'N/A', 6) },
  ]

  return (
    <>
      <div className="flex items-center gap-3 border-t border-slate-700 bg-slate-900/40 p-4">
        <div
          aria-hidden="true"
          className="flex size-14 shrink-0 items-center justify-center rounded-xl border border-blue-500/40 bg-blue-950 font-display text-2xl font-bold text-white"
        >
          {initialOf(nickname)}
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-base font-bold text-white">{nickname}</h2>
          <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-slate-400">
            <span className="truncate">UID: {uid}</span>
            <button
              type="button"
              onClick={copyUid}
              aria-label="Copy UID"
              className="shrink-0 rounded p-1 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-blue-500"
            >
              <Copy className="size-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
        <span className="shrink-0 rounded-lg border border-blue-500/60 bg-blue-500/10 px-3 py-1.5 font-display text-sm font-bold text-blue-400">
          Lv.{formatNumber(account.level)}
        </span>
      </div>

      <dl className="flex flex-col gap-2.5 border-t border-slate-700 p-4">
        {details.map((d) => (
          <div key={d.label} className="flex min-w-0 items-center gap-4">
            <dt className="w-24 shrink-0 font-display text-[11px] font-bold uppercase tracking-[0.15em] text-slate-400">
              {d.label}
            </dt>
            <dd className="min-w-0 truncate text-sm font-medium text-white">{d.value}</dd>
          </div>
        ))}
      </dl>

      <p className="flex items-center gap-2 border-t border-slate-700 px-4 py-3 text-xs text-slate-400">
        <span className="size-1.5 rounded-full bg-slate-500" aria-hidden="true" />
        Updated {timeAgo(account.last_updated_at)}
      </p>
    </>
  )
}
