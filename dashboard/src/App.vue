<script setup>
import { computed, onMounted, ref } from 'vue'
import EquityChart from './components/EquityChart.vue'
import TradesTable from './components/TradesTable.vue'
import { configError, loadAll } from './supabase.js'
import { dateTime, pct, price, qty, rm, tone } from './format.js'
import { stockName } from './names.js'

const data = ref(null)
const error = ref(configError)
const loading = ref(false)
const loadedAt = ref(null)

async function refresh() {
  if (configError) return
  loading.value = true
  try {
    data.value = await loadAll()
    loadedAt.value = new Date()
    error.value = null
  } catch (e) {
    error.value = `读取数据失败 Failed to load data：${e.message}`
  } finally {
    loading.value = false
  }
}
onMounted(refresh)

const priceMap = computed(() =>
  Object.fromEntries((data.value?.prices ?? []).map((p) => [p.ticker, p])),
)

const initialCash = computed(() => Number(data.value?.account?.initial_cash ?? 10000))
const cash = computed(() => Number(data.value?.account?.cash ?? 0))

const positions = computed(() =>
  (data.value?.positions ?? []).map((p) => {
    const lp = priceMap.value[p.ticker]
    const avg = Number(p.avg_price)
    const now = lp ? Number(lp.close) : null
    const value = p.quantity * (now ?? avg)
    return { ...p, avg, now, priceDate: lp?.price_date, value, pnl: now ? now / avg - 1 : null }
  }),
)

const positionsValue = computed(() => positions.value.reduce((s, p) => s + p.value, 0))
const totalValue = computed(() => cash.value + positionsValue.value)
const totalReturn = computed(() => totalValue.value / initialCash.value - 1)

const snapshots = computed(() => data.value?.snapshots ?? [])
const indexReturn = computed(() => {
  const s = snapshots.value
  if (!s.length) return null
  return Number(s[s.length - 1].benchmark_close) / Number(s[0].benchmark_close) - 1
})
const lastSnapshot = computed(() => snapshots.value.at(-1)?.snapshot_date ?? '—')

const statusLabel = { success: '成功 Success', skipped: '跳过 Skipped', failed: '失败 Failed' }
</script>

<template>
  <div class="ribbon" role="note">
    ⚠️ 模拟交易｜虚拟资金 · Paper trading with virtual money only — 不是真实交易 Not real trades
  </div>

  <div class="wrap">
    <header class="top">
      <div>
        <h1>AiTradingMalaysia</h1>
        <p class="muted sub">马股模拟交易 Bursa Malaysia paper trading · 50MA / 200MA</p>
      </div>
      <div class="top-actions">
        <span v-if="loadedAt" class="muted small">更新 Updated {{ dateTime(loadedAt) }}</span>
        <button type="button" :disabled="loading || !!configError" @click="refresh">
          {{ loading ? '读取中 Loading…' : '重新整理 Refresh' }}
        </button>
      </div>
    </header>

    <div v-if="error" class="card alert" role="alert">{{ error }}</div>

    <template v-if="data">
      <section class="metrics">
        <div class="card metric">
          <div class="label">总资产 Total value</div>
          <div class="value num">{{ rm(totalValue) }}</div>
          <div class="muted small">初始 Initial {{ rm(initialCash) }}</div>
        </div>
        <div class="card metric">
          <div class="label">总收益率 Total return</div>
          <div class="value num" :class="tone(totalReturn)">{{ pct(totalReturn) }}</div>
          <div class="muted small">盈亏 P/L {{ rm(totalValue - initialCash) }}</div>
        </div>
        <div class="card metric">
          <div class="label">指数同期收益率 Index return</div>
          <div class="value num" :class="tone(indexReturn)">{{ pct(indexReturn) }}</div>
          <div class="muted small">FBM KLCI · 至 to {{ lastSnapshot }}</div>
        </div>
        <div class="card metric">
          <div class="label">现金 Cash</div>
          <div class="value num">{{ rm(cash) }}</div>
          <div class="muted small">持仓 Positions {{ positions.length }}</div>
        </div>
      </section>

      <section class="section card">
        <div class="section-head">
          <h2>收益曲线<small>Equity curve · Bot vs Index</small></h2>
        </div>
        <EquityChart :snapshots="snapshots" :initial-cash="initialCash" />
      </section>

      <section class="section card">
        <div class="section-head"><h2>持仓<small>Positions</small></h2></div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>代码<small>Ticker</small></th>
                <th class="r">数量<small>Qty</small></th>
                <th class="r">平均成本<small>Avg cost</small></th>
                <th class="r">现价<small>Last close</small></th>
                <th class="r">市值<small>Market value</small></th>
                <th class="r">盈亏<small>P/L %</small></th>
                <th>买入日期<small>Since</small></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in positions" :key="p.ticker">
                <td>{{ p.ticker }} <span class="muted">{{ stockName(p.ticker) }}</span></td>
                <td class="r num">{{ qty(p.quantity) }}</td>
                <td class="r num">{{ price(p.avg) }}</td>
                <td class="r num">
                  <template v-if="p.now">{{ price(p.now) }} <span class="muted small">{{ p.priceDate }}</span></template>
                  <span v-else class="muted">—</span>
                </td>
                <td class="r num">{{ rm(p.value) }}</td>
                <td class="r num" :class="tone(p.pnl)">{{ pct(p.pnl) }}</td>
                <td class="num">{{ p.buy_date }}</td>
              </tr>
              <tr v-if="!positions.length"><td colspan="7" class="empty">目前没有持仓 No open positions</td></tr>
            </tbody>
          </table>
        </div>

        <template v-if="data.pending.length">
          <h3 class="sub-h">待执行挂单 <small class="muted">Pending orders (next trading day at open)</small></h3>
          <div class="table-scroll">
            <table>
              <thead>
                <tr><th>代码 Ticker</th><th>方向 Side</th><th class="r">数量 Qty</th><th>信号日 Signal date</th><th>原因 Reason</th></tr>
              </thead>
              <tbody>
                <tr v-for="o in data.pending" :key="o.id">
                  <td>{{ o.ticker }} <span class="muted">{{ stockName(o.ticker) }}</span></td>
                  <td><span class="badge" :class="o.side.toLowerCase()">{{ o.side }}</span></td>
                  <td class="r num">{{ qty(o.quantity) }}</td>
                  <td class="num">{{ o.signal_date }}</td>
                  <td>{{ o.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </section>

      <section class="section card">
        <div class="section-head"><h2>交易记录<small>Trades</small></h2></div>
        <TradesTable :trades="data.trades" />
      </section>

      <section class="section card">
        <div class="section-head"><h2>运行状态<small>Recent bot runs</small></h2></div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>日期<small>Run date</small></th>
                <th>状态<small>Status</small></th>
                <th>讯息<small>Message</small></th>
                <th>结束时间<small>Finished (MYT)</small></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in data.runs" :key="r.id">
                <td class="num">{{ r.run_date }}</td>
                <td><span class="badge" :class="r.status">{{ statusLabel[r.status] ?? r.status }}</span></td>
                <td class="msg">{{ r.message }}</td>
                <td class="num muted">{{ dateTime(r.finished_at) }}</td>
              </tr>
              <tr v-if="!data.runs.length"><td colspan="4" class="empty">还没有运行记录 No runs yet</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <p v-else-if="loading" class="muted loading">读取中 Loading…</p>

    <footer class="muted small foot">
      AiTradingMalaysia · 仅供学习研究，不构成投资建议 · For learning only, not investment advice
    </footer>
  </div>
</template>

<style scoped>
.ribbon {
  background: var(--warn-bg); color: var(--warn-text); border-bottom: 1px solid var(--warn-border);
  text-align: center; font-weight: 600; padding: 8px 16px; font-size: 13px;
  position: sticky; top: 0; z-index: 10;
}
.top { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; padding: 22px 0 8px; }
h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
.sub { margin: 2px 0 0; }
.top-actions { display: flex; align-items: center; gap: 10px; }
.small { font-size: 12px; }
.alert { border-color: var(--warn-border); background: var(--warn-bg); color: var(--warn-text); margin-top: 12px; }

.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.metric .label { font-size: 12px; color: var(--muted); font-weight: 600; }
.metric .value { font-size: 24px; font-weight: 700; margin: 4px 0 2px; letter-spacing: -0.01em; }
@media (max-width: 860px) { .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 420px) { .metric .value { font-size: 19px; } }

.sub-h { font-size: 14px; margin: 18px 0 8px; }
.msg { white-space: normal; min-width: 220px; }
.loading { padding: 40px 0; text-align: center; }
.foot { margin-top: 28px; text-align: center; }
</style>
