import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Loader2 } from 'lucide-react'

type Variant = 'primary' | 'outline' | 'neon' | 'danger' | 'ghost'

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-white text-slate-900 hover:bg-slate-200 border-white',
  outline: 'border-slate-700 bg-slate-800 text-slate-200 hover:border-slate-500 hover:text-white',
  neon: 'border-neon/50 bg-neon/10 text-neon hover:bg-neon/20 hover:border-neon',
  danger: 'border-danger/60 bg-danger/10 text-danger hover:bg-danger/20 hover:border-danger',
  ghost: 'border-transparent bg-transparent text-slate-400 hover:text-white',
}

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant
  loading?: boolean
  icon?: ReactNode
}

export function Button({ variant = 'outline', loading = false, icon, className = '', children, disabled, ...rest }: Props) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`inline-flex min-h-11 min-w-0 items-center justify-center gap-2 rounded-xl border px-4 text-sm font-bold uppercase tracking-wide transition-all duration-200 active:scale-[0.97] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100 ${VARIANTS[variant]} ${className}`}
    >
      {loading ? <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden="true" /> : icon}
      {children ? <span className="truncate">{children}</span> : null}
    </button>
  )
}
