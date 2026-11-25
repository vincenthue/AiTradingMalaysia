// 把一个市场的原始数据算成画面要用的数字（总资产、收益率、持仓盈亏、曲线）
export function summarize(m) {
  const priceMap = Object.fromEntries(m.prices.map((p) => [p.ticker, p]))
  const initial = Number(m.account?.initial_cash ?? 0)
  const cash = Number(m.account?.cash ?? 0)

  const positions = m.positions.map((p) => {
    const lp = priceMap[p.ticker]
    const avg = Number(p.avg_price)
    const now = lp ? Number(lp.close) : null
    const value = p.quantity * (now ?? avg)
    return { ...p, avg, now, priceDate: lp?.price_date, value, pnl: now ? now / avg - 1 : null }
  })
  const positionsValue = positions.reduce((s, p) => s + p.value, 0)
  const totalValue = cash + positionsValue

  const snaps = m.snapshots
  const b0 = snaps.length ? Number(snaps[0].benchmark_close) : null
  const indexReturn = snaps.length ? Number(snaps.at(-1).benchmark_close) / b0 - 1 : null
  const round2 = (v) => +v.toFixed(2)

  return {
    ...m,
    initial,
    cash,
    positions,
    positionsValue,
    totalValue,
    pnl: totalValue - initial,
    totalReturn: initial ? totalValue / initial - 1 : null,
    indexReturn,
    lastSnapshot: snaps.at(-1)?.snapshot_date ?? null,
    lastRun: m.runs[0] ?? null,
    // 收益率曲线（%）：[日期, 数值]
    equityCurve: snaps.map((s) => [s.snapshot_date, round2((Number(s.total_value) / initial - 1) * 100)]),
    indexCurve: snaps.map((s) => [s.snapshot_date, round2((Number(s.benchmark_close) / b0 - 1) * 100)]),
  }
}
