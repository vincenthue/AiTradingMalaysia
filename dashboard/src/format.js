const num = (v) => Number(v ?? 0)

const fmt2 = (v) => num(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

// 金额 + 该市场货币符号：RM 1,000.00 / $1,000.00；signed → +RM 5.00 / -$3.20
export const money = (v, symbol = 'RM', signed = false) => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  const n = num(v)
  const sep = /[A-Za-z]$/.test(symbol) ? ' ' : ''
  const sign = signed ? (n >= 0 ? '+' : '-') : n < 0 ? '-' : ''
  return `${sign}${symbol}${sep}${fmt2(Math.abs(n))}`
}

export const price = (v) => num(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })

export const pct = (v, signed = true) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const s = (v * 100).toFixed(2) + '%'
  return signed && v > 0 ? '+' + s : s
}

export const tone = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '')

export const dateTime = (v) =>
  v ? new Date(v).toLocaleString('en-MY', { timeZone: 'Asia/Kuala_Lumpur', hour12: false }) : '—'

export const qty = (v) => num(v).toLocaleString('en-US')
