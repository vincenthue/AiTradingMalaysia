const num = (v) => Number(v ?? 0)

export const rm = (v) =>
  'RM ' + num(v).toLocaleString('en-MY', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const price = (v) => num(v).toLocaleString('en-MY', { minimumFractionDigits: 2, maximumFractionDigits: 4 })

export const pct = (v, signed = true) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const s = (v * 100).toFixed(2) + '%'
  return signed && v > 0 ? '+' + s : s
}

export const tone = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '')

export const dateTime = (v) =>
  v ? new Date(v).toLocaleString('en-MY', { timeZone: 'Asia/Kuala_Lumpur', hour12: false }) : '—'

export const qty = (v) => num(v).toLocaleString('en-MY')
