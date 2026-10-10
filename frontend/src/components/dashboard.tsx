import { useCallback, useState } from 'react'
import useSWR from 'swr'
import { api, clearToken, fetcher, type Stats, type User } from '../lib/api'
import { formatDuration, safeText } from '../lib/format'
import { AppHeader } from './app-header'
import { AccountCard } from './account/account-card'
import { DashboardSkeleton, EmptyState, ErrorState } from './ui/states'
import { useToast } from './ui/toast'

export function Dashboard() {
  const toast = useToast()
  const [refreshingAll, setRefreshingAll] = useState(false)

  const { data: me } = useSWR<{ user: User }>('/api/user/me', fetcher)
  const { data, error, isLoading, mutate } = useSWR<Stats>('/api/stats', fetcher, {
    refreshInterval: 10000,
    revalidateOnFocus: true,
    dedupingInterval: 3000,
  })

  const revalidate = useCallback(() => {
    window.setTimeout(() => void mutate(), 800)
  }, [mutate])

  const refreshAll = async () => {
    setRefreshingAll(true)
    try {
      const res = await api.refreshAll()
      toast(`Refreshed ${res.refreshed ?? 0} account(s)`, 'success')
      await mutate()
    } catch (err) {
      toast(err instanceof Error ? err.message : 'Refresh failed', 'error')
    } finally {
      setRefreshingAll(false)
    }
  }

  const logout = async () => {
    await api.logout()
    clearToken()
  }

  const slotsTotal = data?.user_slots ?? 0
  const slots = data ? `${data.total_accounts}/${slotsTotal >= 9999 ? '∞' : slotsTotal}` : '—'
  const timeLeft = !data ? '—' : data.plan_expired ? 'Expired' : formatDuration(data.remaining_seconds)

  return (
    <div className="min-h-dvh">
      <AppHeader
        username={safeText(me?.user.display_name || me?.user.username, '—', 16)}
        timeLeft={timeLeft}
        slots={slots}
        loading={isLoading}
        refreshing={refreshingAll}
        onRefresh={refreshAll}
        onLogout={logout}
      />

      <main className="mx-auto flex w-full max-w-lg flex-col gap-4 px-4 py-4 pb-[max(1.5rem,env(safe-area-inset-bottom))]">
        <h2 className="sr-only">Your accounts</h2>
        {isLoading ? (
          <DashboardSkeleton />
        ) : error && !data ? (
          <ErrorState message={error instanceof Error ? error.message : 'Failed to load'} onRetry={() => void mutate()} />
        ) : !data?.accounts.length ? (
          <EmptyState />
        ) : (
          data.accounts.map((account) => <AccountCard key={account.uid} account={account} onChanged={revalidate} />)
        )}
      </main>
    </div>
  )
}
