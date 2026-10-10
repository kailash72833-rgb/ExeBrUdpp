// No secrets live in the frontend: the only credential is the per-user session
// token issued by the backend after login. All privileged logic stays server-side.
const TOKEN_KEY = 'nxc_token'
const REQUEST_TIMEOUT_MS = 15000

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string) {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* storage unavailable (private mode) – session lives in memory only */
  }
  window.dispatchEvent(new Event('auth-change'))
}

export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
  window.dispatchEvent(new Event('auth-change'))
}

type ApiEnvelope = { status: 'ok' | 'error'; error?: string; message?: string }

async function request<T extends object>(path: string, init: RequestInit = {}): Promise<T & ApiEnvelope> {
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  try {
    const res = await fetch(path, {
      ...init,
      headers,
      signal: controller.signal,
      credentials: 'same-origin',
      cache: 'no-store',
    })

    let data: (T & ApiEnvelope) | null = null
    try {
      data = (await res.json()) as T & ApiEnvelope
    } catch {
      throw new ApiError('Invalid server response', res.status)
    }

    if (res.status === 401) {
      clearToken()
      throw new ApiError('Session expired. Please log in again.', 401)
    }
    if (!res.ok || data.status !== 'ok') {
      throw new ApiError(data.error || 'Request failed', res.status)
    }
    return data
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('Request timed out', 0)
    }
    if (err instanceof ApiError) throw err
    throw new ApiError('Network error. Check your connection.', 0)
  } finally {
    window.clearTimeout(timer)
  }
}

export type Account = {
  uid: string
  nickname?: string
  region?: string
  level?: number
  initial_exp?: number
  current_exp?: number
  gained_exp?: number
  status?: string
  mode?: string
  matches_played?: number
  active_matches?: number
  last_updated_at?: number
  added_at?: number
  is_paused?: boolean
}

export type Stats = {
  total_accounts: number
  total_matches: number
  total_gained_exp: number
  accounts: Account[]
  plan_name: string
  user_slots: number
  available_slots: number
  remaining_seconds: number
  plan_expired: boolean
}

export type User = { username: string; display_name?: string; plan?: string }

export const fetcher = <T extends object>(path: string) => request<T>(path)

export const api = {
  login: (identity: string, password: string) =>
    request<{ token: string; user: User }>('/api/user/login', {
      method: 'POST',
      body: JSON.stringify({ identity, username: identity, password }),
    }),
  logout: () => request<object>('/api/user/logout', { method: 'POST' }).catch(() => null),
  pause: (uid: string) => request<object>('/api/account/pause', { method: 'POST', body: JSON.stringify({ uid }) }),
  refresh: (uid: string) => request<object>('/api/account/refresh', { method: 'POST', body: JSON.stringify({ uid }) }),
  refreshAll: () => request<{ refreshed?: number }>('/api/account/refresh-all', { method: 'POST', body: '{}' }),
  remove: (uid: string) => request<object>('/api/account/delete', { method: 'POST', body: JSON.stringify({ uid }) }),
}
