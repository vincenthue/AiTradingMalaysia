<script setup>
// 总览：每个市场一张卡片（各自货币），加上所有市场的收益率曲线（用 %，不换算汇率）
import { computed } from 'vue'
import ReturnChart from './ReturnChart.vue'
import { dateTime, money, pct, tone } from '../format.js'

const props = defineProps({ markets: { type: Array, required: true } })
const emit = defineEmits(['open'])

const statusLabel = { success: '成功 Success', skipped: '跳过 Skipped', failed: '失败 Failed' }

const series = computed(() =>
  props.markets.flatMap((m) => [
    { name: `${m.meta.flag} ${m.meta.zh} Bot (${m.code})`, data: m.equityCurve, color: m.meta.color },
    // 指数默认隐藏（点图例可以打开），避免线太多
    { name: `${m.meta.index} (${m.code})`, data: m.indexCurve, color: m.meta.color, dashed: true, hidden: true },
  ]),
)

// 跑赢 / 跑输指数（百分点）
const vsIndex = (m) =>
  m.totalReturn === null || m.indexReturn === null ? null : m.totalReturn - m.indexReturn
</script>

<template>
  <section class="cards">
    <button
      v-for="m in markets" :key="m.code" type="button" class="card mcard"
      :aria-label="`打开 ${m.meta.zh} ${m.code}`" @click="emit('open', m.code)"
    >
      <div class="mhead">
        <span class="mname">{{ m.meta.flag }} {{ m.meta.zh }} <span class="muted">{{ m.meta.en }}</span></span>
        <span class="chev" aria-hidden="true">›</span>
      </div>
      <div class="big num">{{ money(m.totalValue, m.meta.symbol) }}</div>
      <div class="muted small">初始 Initial {{ money(m.initial, m.meta.symbol) }} · {{ m.meta.currency }}</div>

      <dl class="kv">
        <div>
          <dt>总收益率 Return</dt>
          <dd class="num" :class="tone(m.totalReturn)">{{ pct(m.totalReturn) }}</dd>
        </div>
        <div>
          <dt>{{ m.meta.index }}</dt>
          <dd class="num" :class="tone(m.indexReturn)">{{ pct(m.indexReturn) }}</dd>
        </div>
        <div>
          <dt>对比指数 vs Index</dt>
          <dd class="num" :class="tone(vsIndex(m))">{{ pct(vsIndex(m)) }}</dd>
        </div>
        <div>
          <dt>持仓 Positions</dt>
          <dd class="num">{{ m.positions.length }}</dd>
        </div>
      </dl>

      <div class="run">
        <template v-if="m.lastRun">
          <span class="badge" :class="m.lastRun.status">{{ statusLabel[m.lastRun.status] ?? m.lastRun.status }}</span>
          <span class="muted small">最近运行 Last run {{ m.lastRun.run_date }} · {{ dateTime(m.lastRun.finished_at) }} MYT</span>
        </template>
        <span v-else class="muted small">还没有运行记录 No runs yet</span>
      </div>
    </button>
  </section>

  <section class="section card">
    <div class="section-head">
      <h2>各市场收益率<small>Returns by market (%, no FX conversion)</small></h2>
      <span class="muted small">点图例可显示指数 Click legend to show indices</span>
    </div>
    <ReturnChart :series="series" label="各市场收益率 Returns by market" />
  </section>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-top: 14px; }
.mcard {
  text-align: left; font: inherit; color: inherit; cursor: pointer; display: block; width: 100%;
  transition: border-color 0.15s ease;
}
.mcard:hover { border-color: var(--accent); background: var(--surface); }
.mhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.mname { font-weight: 650; font-size: 15px; }
.chev { color: var(--muted); font-size: 20px; line-height: 1; }
.big { font-size: 26px; font-weight: 700; letter-spacing: -0.01em; margin: 8px 0 2px; }
.small { font-size: 12px; }
.kv { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 16px; margin: 14px 0 12px; }
.kv dt { font-size: 12px; color: var(--muted); font-weight: 600; }
.kv dd { margin: 2px 0 0; font-size: 16px; font-weight: 650; }
.run { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; border-top: 1px solid var(--border); padding-top: 10px; }
</style>
