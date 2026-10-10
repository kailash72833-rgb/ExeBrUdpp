import { memo } from 'react'
import type { Account } from '../../lib/api'
import { AccountTags } from './account-tags'
import { ProfileSection } from './profile-section'
import { StatsGrid } from './stats-grid'
import { ActionControls } from './action-controls'

export const AccountCard = memo(function AccountCard({ account, onChanged }: { account: Account; onChanged: () => void }) {
  return (
    <article className="rounded-xl border border-slate-700 bg-slate-800 transition-colors hover:border-slate-600">
      <div className="p-4">
        <AccountTags account={account} />
      </div>
      <ProfileSection account={account} />
      <StatsGrid account={account} />
      <ActionControls account={account} onChanged={onChanged} />
    </article>
  )
})
