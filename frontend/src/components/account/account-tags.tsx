import { useId, useState } from 'react'
import { Check, ChevronDown, Globe, IdCard } from 'lucide-react'
import { MODE_LABELS, formatNumber, modeLabel, safeText } from '../../lib/format'
import type { Account } from '../../lib/api'

function statusInfo(account: Account) {
  if (account.is_paused) return { label: 'Paused', dot: 'bg-slate-400', ring: 'border-slate-600 text-slate-300', glow: false }
  const status = (account.status || 'OFFLINE').toUpperCase()
  if (status === 'ONLINE') return { label: 'Online', dot: 'bg-neon', ring: 'border-neon/50 text-neon', glow: true }
  if (status.startsWith('IN MATCH')) return { label: 'In Match', dot: 'bg-blue-400', ring: 'border-blue-500/60 text-blue-400', glow: false }
  return { label: safeText(status, 'Offline', 16), dot: 'bg-slate-500', ring: 'border-slate-600 text-slate-400', glow: false }
}

const pill = 'inline-flex h-8 min-w-0 items-center gap-1.5 rounded-full border px-3 text-xs font-bold uppercase tracking-wide'

export function AccountTags({ account }: { account: Account }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const status = statusInfo(account)
  const currentMode = (account.mode || 'auto').toLowerCase()

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`${pill} border-slate-600 text-slate-200`}>
          <Globe className="size-3.5 shrink-0 text-slate-400" aria-hidden="true" />
          {safeText(account.region, 'N/A', 6)}
        </span>
        <span className={`${pill} max-w-[11rem] border-slate-600 text-slate-200`}>
          <IdCard className="size-3.5 shrink-0 text-slate-400" aria-hidden="true" />
          <span className="truncate">{safeText(account.uid, '—', 20)}</span>
        </span>
        <span className={`${pill} ml-auto ${status.ring}`}>
          <span className={`size-2 shrink-0 rounded-full ${status.dot} ${status.glow ? 'glow-dot' : ''}`} aria-hidden="true" />
          {status.label}
        </span>
      </div>

      <div className="relative self-start">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((v) => !v)}
          className={`${pill} h-9 border-neon/50 bg-neon/10 text-neon transition-colors hover:bg-neon/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500`}
        >
          <Check className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="font-display">{modeLabel(account.mode)}</span>
          <span className="text-neon/70">{formatNumber(account.matches_played)}</span>
          <ChevronDown className={`size-3.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
        </button>

        {open ? (
          <ul
            id={panelId}
            className="absolute left-0 top-full z-10 mt-2 w-56 overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-xl"
          >
            {Object.entries(MODE_LABELS).map(([key, label]) => {
              const active = key === currentMode
              return (
                <li
                  key={key}
                  className={`flex items-center justify-between px-4 py-2.5 text-xs font-bold uppercase tracking-wide ${active ? 'text-neon' : 'text-slate-500'}`}
                >
                  <span>{label}</span>
                  {active ? <Check className="size-3.5" aria-label="Current mode" /> : null}
                </li>
              )
            })}
            <li className="border-t border-slate-700 px-4 py-2 text-[11px] leading-relaxed text-slate-400">
              Active matches: {formatNumber(account.active_matches)}
            </li>
          </ul>
        ) : null}
      </div>
    </div>
  )
}
