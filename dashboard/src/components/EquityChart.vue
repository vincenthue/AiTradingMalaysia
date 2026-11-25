<script setup>
// 收益曲线：bot 总收益率 vs 指数收益率（从 daily_snapshots）
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, SVGRenderer])

const props = defineProps({
  snapshots: { type: Array, required: true },
  initialCash: { type: Number, required: true },
})

const el = ref(null)
let chart = null
const media = window.matchMedia('(prefers-color-scheme: dark)')

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

// 图表容器只在有 >= 2 天数据时才存在；按需建立 / 释放
function ensureChart() {
  if (!el.value) {
    chart?.dispose()
    chart = null
    return false
  }
  if (!chart || chart.getDom() !== el.value) {
    chart?.dispose()
    chart = echarts.init(el.value, null, { renderer: 'svg' })
  }
  return true
}

function render() {
  if (!ensureChart()) return
  const snaps = props.snapshots
  const b0 = snaps.length ? Number(snaps[0].benchmark_close) : 1
  const dates = snaps.map((s) => s.snapshot_date)
  const bot = snaps.map((s) => +((Number(s.total_value) / props.initialCash - 1) * 100).toFixed(2))
  const idx = snaps.map((s) => +((Number(s.benchmark_close) / b0 - 1) * 100).toFixed(2))
  const text = cssVar('--text'), muted = cssVar('--muted'), border = cssVar('--border')
  const surface = cssVar('--surface')

  chart.setOption({
    animation: false,
    grid: { left: 8, right: 16, top: 36, bottom: snaps.length > 40 ? 52 : 12, containLabel: true },
    legend: { top: 0, left: 0, textStyle: { color: text }, itemWidth: 18, itemHeight: 3 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: surface, borderColor: border, textStyle: { color: text },
      valueFormatter: (v) => (v > 0 ? '+' : '') + v.toFixed(2) + '%',
    },
    xAxis: {
      type: 'category', data: dates, boundaryGap: false,
      axisLine: { lineStyle: { color: border } }, axisLabel: { color: muted },
    },
    yAxis: {
      type: 'value', scale: true,
      axisLabel: { color: muted, formatter: (v) => v + '%' },
      splitLine: { lineStyle: { color: border } },
    },
    dataZoom: snaps.length > 40 ? [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }] : [],
    series: [
      {
        name: 'Bot 模拟组合 (Portfolio)', type: 'line', data: bot, showSymbol: snaps.length < 30,
        lineStyle: { width: 2.5, color: cssVar('--line-bot') }, itemStyle: { color: cssVar('--line-bot') },
      },
      {
        name: 'FBM KLCI 指数 (Index)', type: 'line', data: idx, showSymbol: snaps.length < 30,
        lineStyle: { width: 2, color: cssVar('--line-index') }, itemStyle: { color: cssVar('--line-index') },
      },
    ],
  }, true)
}

const onResize = () => chart?.resize()
const onTheme = () => render()

onMounted(() => {
  render()
  window.addEventListener('resize', onResize)
  media.addEventListener('change', onTheme)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  media.removeEventListener('change', onTheme)
  chart?.dispose()
})
watch(() => [props.snapshots, props.initialCash], render, { deep: true, flush: 'post' })
</script>

<template>
  <div v-if="snapshots.length < 2" class="empty">
    需要至少两天的数据才能画收益曲线（目前 {{ snapshots.length }} 天）<br />
    <small>The chart appears after at least 2 trading days.</small>
  </div>
  <div v-else ref="el" class="chart" role="img" aria-label="收益曲线 Equity curve: bot vs index"></div>
</template>

<style scoped>
.chart { width: 100%; height: 320px; }
</style>
