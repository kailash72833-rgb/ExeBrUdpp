import { formatNumber, safeNumber } from '../../lib/format'
import type { Account } from '../../lib/api'

export function StatsGrid({ account }: { account: Account }) {
  const gained = safeNumber(account.gained_exp)
  const cells = [
    { label: 'EXP', value: formatNumber(account.current_exp), tone: 'text-white' },
    { label: 'Initial', value: formatNumber(account.initial_exp), tone: 'text-white' },
    { label: 'Gained', value: `+${formatNumber(gained)}`, tone: gained > 0 ? 'text-neon' : 'text-slate-300' },
  ]

  return (
    <section aria-label="Level and experience" className="border-t border-slate-700">
      <div className="flex items-center justify-between px-4 py-3.5">
        <span className="font-display text-sm font-bold uppercase tracking-[0.2em] text-slate-400">Level</span>
        <span className="font-display text-2xl font-extrabold text-white">{formatNumber(account.level)}</span>
      </div>
      <dl className="grid grid-cols-3 divide-x divide-slate-700 border-t border-slate-700 bg-slate-900/40">
        {cells.map((c) => (
          <div key={c.label} className="flex min-w-0 flex-col items-center gap-1 px-2 py-3.5">
            <dt className="font-display text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{c.label}</dt>
            <dd className={`max-w-full truncate text-base font-bold ${c.tone}`}>{c.value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex items-center justify-between border-t border-slate-700 px-4 py-3.5">
        <span className="font-display text-xs font-bold uppercase tracking-[0.2em] text-slate-400">Matches Played</span>
        <span className="text-lg font-bold text-white">{formatNumber(account.matches_played)}</span>
      </div>
    </section>
  )
}
