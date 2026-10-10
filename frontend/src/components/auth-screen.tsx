import { useState, type FormEvent } from 'react'
import { Eye, EyeOff, LogIn } from 'lucide-react'
import { api, setToken } from '../lib/api'
import { sanitizeInput } from '../lib/format'
import { Button } from './ui/button'

const inputClass =
  'h-12 w-full rounded-xl border border-slate-700 bg-slate-900 px-4 text-sm text-white placeholder:text-slate-500 transition-colors focus:border-blue-500 focus:outline-none'

export function AuthScreen() {
  const [identity, setIdentity] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (loading) return
    const cleanIdentity = sanitizeInput(identity, 64).toLowerCase()
    if (!cleanIdentity || !password) {
      setError('Enter your username/Gmail and password.')
      return
    }
    if (password.length > 128) {
      setError('Password is too long.')
      return
    }
    setLoading(true)
    setError('')
    try {
      const res = await api.login(cleanIdentity, password)
      setPassword('')
      setToken(res.token)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm rounded-xl border border-slate-700 bg-slate-800 p-6">
        <div className="mb-6 flex items-center gap-3">
          <div aria-hidden="true" className="flex size-11 items-center justify-center rounded-xl bg-blue-600 font-display text-xl font-extrabold">
            K
          </div>
          <div className="min-w-0">
            <h1 className="truncate font-display text-base font-bold tracking-wider">Krishna Coder LevelUp</h1>
            <p className="text-xs text-slate-400">Sign in to your dashboard</p>
          </div>
        </div>

        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="identity" className="font-display text-[11px] font-bold uppercase tracking-[0.15em] text-slate-400">
              Username or Gmail
            </label>
            <input
              id="identity"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={64}
              value={identity}
              onChange={(e) => setIdentity(e.target.value)}
              className={inputClass}
              placeholder="you@gmail.com"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="password" className="font-display text-[11px] font-bold uppercase tracking-[0.15em] text-slate-400">
              Password
            </label>
            <div className="relative">
              <input
                id="password"
                name="password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                maxLength={128}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={`${inputClass} pr-12`}
                placeholder="••••••••"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute inset-y-0 right-0 flex w-12 items-center justify-center text-slate-400 transition-colors hover:text-white"
              >
                {showPassword ? <EyeOff className="size-4" aria-hidden="true" /> : <Eye className="size-4" aria-hidden="true" />}
              </button>
            </div>
          </div>

          {error ? (
            <p role="alert" className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger break-words">
              {error}
            </p>
          ) : null}

          <Button type="submit" variant="primary" loading={loading} icon={<LogIn className="size-4" aria-hidden="true" />}>
            Login
          </Button>
          <p className="text-center text-xs text-slate-400">
            {"Don't have an account? "}
            <a href="/" className="font-bold text-blue-400 hover:text-blue-300">
              Register
            </a>
          </p>
        </form>
      </div>
    </main>
  )
}
