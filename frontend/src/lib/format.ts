const CONTROL_CHARS = /[\u0000-\u001F\u007F\u200B-\u200F\u202A-\u202E\u2066-\u2069]/g

/** Strips control / bidi-override characters and bounds length for user input. */
export function sanitizeInput(value: string, maxLength = 128): string {
  return value.replace(CONTROL_CHARS, '').trim().slice(0, maxLength)
}

/** Safe display text for server data. React escapes HTML; this removes spoofing chars and caps length. */
export function safeText(value: unknown, fallback = '—', maxLength = 48): string {
  if (value === null || value === undefined) return fallback
  const text = String(value).replace(CONTROL_CHARS, '').trim()
  return text ? text.slice(0, maxLength) : fallback
}

export function safeNumber(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : 0
}

const numberFormatter = new Intl.NumberFormat('en-US')
export const formatNumber = (value: unknown) => numberFormatter.format(safeNumber(value))

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(safeNumber(seconds)))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m`
}

export function timeAgo(unixSeconds?: number): string {
  const ts = safeNumber(unixSeconds)
  if (!ts) return '—'
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (diff < 60) return 'Just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

export function initialOf(name: string): string {
  const first = Array.from(name.trim())[0]
  return first ? first.toUpperCase() : '?'
}

export const MODE_LABELS: Record<string, string> = {
  auto: 'Auto',
  br: 'Battle Royale',
  lw: 'Lone Wolf',
}

export function modeLabel(mode?: string): string {
  return MODE_LABELS[(mode || 'auto').toLowerCase()] ?? safeText(mode, 'Auto', 20)
}
