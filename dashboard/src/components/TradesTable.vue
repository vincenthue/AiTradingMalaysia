<script setup>
// 交易记录（可按日期和代码筛选）
import { computed, ref } from 'vue'
import { money, price, qty } from '../format.js'
import { stockName } from '../names.js'

const props = defineProps({
  trades: { type: Array, required: true },
  symbol: { type: String, default: 'RM' },
})

const ticker = ref('')
const from = ref('')
const to = ref('')

const tickers = computed(() => [...new Set(props.trades.map((t) => t.ticker))].sort())

const rows = computed(() =>
  props.trades.filter(
    (t) =>
      (!ticker.value || t.ticker === ticker.value) &&
      (!from.value || t.trade_date >= from.value) &&
      (!to.value || t.trade_date <= to.value),
  ),
)

const reset = () => { ticker.value = ''; from.value = ''; to.value = '' }
</script>

<template>
  <div class="filters">
    <label>代码 Ticker
      <select v-model="ticker">
        <option value="">全部 All</option>
        <option v-for="t in tickers" :key="t" :value="t">{{ t }} {{ stockName(t) }}</option>
      </select>
    </label>
    <label>从 From <input v-model="from" type="date" /></label>
    <label>到 To <input v-model="to" type="date" /></label>
    <button type="button" @click="reset">清除 Reset</button>
    <span class="muted count">{{ rows.length }} 笔 trades</span>
  </div>

  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>日期<small>Date</small></th>
          <th>代码<small>Ticker</small></th>
          <th>方向<small>Side</small></th>
          <th class="r">数量<small>Qty</small></th>
          <th class="r">价格<small>Price</small></th>
          <th class="r">金额<small>Value</small></th>
          <th class="r">费用<small>Fees</small></th>
          <th>原因<small>Reason</small></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in rows" :key="t.id">
          <td class="num">{{ t.trade_date }}</td>
          <td>{{ t.ticker }} <span class="muted">{{ stockName(t.ticker) }}</span></td>
          <td><span class="badge" :class="t.side.toLowerCase()">{{ t.side === 'BUY' ? '买 BUY' : '卖 SELL' }}</span></td>
          <td class="r num">{{ qty(t.quantity) }}</td>
          <td class="r num">{{ price(t.price) }}</td>
          <td class="r num">{{ money(t.quantity * t.price, symbol) }}</td>
          <td class="r num">{{ money(t.fees, symbol) }}</td>
          <td class="reason">{{ t.reason }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="8" class="empty">没有交易记录 No trades</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.filters { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: end; margin-bottom: 12px; }
.filters label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.count { margin-left: auto; font-size: 12px; align-self: center; }
.reason { white-space: normal; min-width: 180px; }
</style>
