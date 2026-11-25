<script setup>
// 单一市场页面（马股 / 美股）：指标卡、收益曲线、持仓、挂单、交易记录、运行状态
import { computed } from 'vue'
import ReturnChart from './ReturnChart.vue'
import TradesTable from './TradesTable.vue'
import { dateTime, money, pct, price, qty, tone } from '../format.js'
import { stockName } from '../names.js'

const props = defineProps({ m: { type: Object, required: true } })

const sym = computed(() => props.m.meta.symbol)
const cur = computed(() => props.m.meta.currency)
const series = computed(() => [
  { name: `Bot ${props.m.meta.zh} (${props.m.code})`, data: props.m.equityCurve, color: props.m.meta.color },
  { name: `${props.m.meta.index} 指数 (Index)`, data: props.m.indexCurve, color: '--line-index', dashed: true },
])
const statusLabel = { success: '成功 Success', skipped: '跳过 Skipped', failed: '失败 Failed' }
</script>

<template>
  <section class="metrics">
    <div class="card metric">
      <div class="label">总资产 Total value</div>
      <div class="value num">{{ money(m.totalValue, sym) }}</div>
      <div class="muted small">初始 Initial {{ money(m.initial, sym) }}</div>
    </div>
    <div class="card metric">
      <div class="label">总收益率 Total return</div>
      <div class="value num" :class="tone(m.totalReturn)">{{ pct(m.totalReturn) }}</div>
      <div class="muted small">盈亏 P/L {{ money(m.pnl, sym, true) }}</div>
    </div>
    <div class="card metric">
      <div class="label">指数同期收益率 Index return</div>
      <div class="value num" :class="tone(m.indexReturn)">{{ pct(m.indexReturn) }}</div>
      <div class="muted small">{{ m.meta.index }} · 至 to {{ m.lastSnapshot ?? '—' }}</div>
    </div>
    <div class="card metric">
      <div class="label">现金 Cash</div>
      <div class="value num">{{ money(m.cash, sym) }}</div>
      <div class="muted small">持仓 Positions {{ m.positions.length }}</div>
    </div>
  </section>

  <section class="section card">
    <div class="section-head">
      <h2>收益曲线<small>Equity curve · Bot vs {{ m.meta.index }}</small></h2>
    </div>
    <ReturnChart :series="series" :label="`收益曲线 ${m.code}: bot vs ${m.meta.index}`" />
  </section>

  <section class="section card">
    <div class="section-head"><h2>持仓<small>Positions · {{ cur }}</small></h2></div>
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
          <tr v-for="p in m.positions" :key="p.ticker">
            <td>{{ p.ticker }} <span class="muted">{{ stockName(p.ticker) }}</span></td>
            <td class="r num">{{ qty(p.quantity) }}</td>
            <td class="r num">{{ price(p.avg) }}</td>
            <td class="r num">
              <template v-if="p.now">{{ price(p.now) }} <span class="muted small">{{ p.priceDate }}</span></template>
              <span v-else class="muted">—</span>
            </td>
            <td class="r num">{{ money(p.value, sym) }}</td>
            <td class="r num" :class="tone(p.pnl)">{{ pct(p.pnl) }}</td>
            <td class="num">{{ p.buy_date }}</td>
          </tr>
          <tr v-if="!m.positions.length"><td colspan="7" class="empty">目前没有持仓 No open positions</td></tr>
        </tbody>
      </table>
    </div>

    <template v-if="m.pending.length">
      <h3 class="sub-h">待执行挂单 <small class="muted">Pending orders (next trading day at open)</small></h3>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th>代码 Ticker</th><th>方向 Side</th><th class="r">数量 Qty</th><th>信号日 Signal date</th><th>原因 Reason</th></tr>
          </thead>
          <tbody>
            <tr v-for="o in m.pending" :key="o.id">
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
    <div class="section-head"><h2>交易记录<small>Trades · {{ cur }}</small></h2></div>
    <TradesTable :trades="m.trades" :symbol="sym" />
  </section>

  <section class="section card">
    <div class="section-head"><h2>运行状态<small>Recent bot runs · {{ m.code }}</small></h2></div>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>交易日<small>Run date ({{ m.code }} local)</small></th>
            <th>状态<small>Status</small></th>
            <th>讯息<small>Message</small></th>
            <th>结束时间<small>Finished (MYT)</small></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in m.runs" :key="r.id">
            <td class="num">{{ r.run_date }}</td>
            <td><span class="badge" :class="r.status">{{ statusLabel[r.status] ?? r.status }}</span></td>
            <td class="msg">{{ r.message }}</td>
            <td class="num muted">{{ dateTime(r.finished_at) }}</td>
          </tr>
          <tr v-if="!m.runs.length"><td colspan="4" class="empty">还没有运行记录 No runs yet</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.sub-h { font-size: 14px; margin: 18px 0 8px; }
.msg { white-space: normal; min-width: 220px; }
</style>
