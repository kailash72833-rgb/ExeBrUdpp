import { useSyncExternalStore } from 'react'
import { getToken } from './lib/api'
import { AuthScreen } from './components/auth-screen'
import { Dashboard } from './components/dashboard'

function subscribe(callback: () => void) {
  window.addEventListener('auth-change', callback)
  window.addEventListener('storage', callback)
  return () => {
    window.removeEventListener('auth-change', callback)
    window.removeEventListener('storage', callback)
  }
}

export function App() {
  const token = useSyncExternalStore(subscribe, getToken, () => '')
  return token ? <Dashboard key={token} /> : <AuthScreen />
}
