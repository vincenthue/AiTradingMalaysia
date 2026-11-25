<script setup>
// 收益率曲线（%）。series: [{ name, data: [[日期, 数值]], color: '--css-变量', dashed, hidden }]
// 用时间轴：不同市场的交易日不一样（假期不同），也能画在同一张图上。
import { onBeforeUnmount, onMounted, ref, watch, computed } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, SVGRenderer])

const props = defineProps({
  series: { type: Array, required: true },
  label: { type: String, default: '收益率曲线 Returns chart' },
})

const el = ref(null)
let chart = null
const media = window.matchMedia('(prefers-color-scheme: dark)')
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

const longest = computed(() => Math.max(0, ...props.series.map((s) => s.data.length)))

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
  const text = cssVar('--text'), muted = cssVar('--muted'), border = cssVar('--border')
  const many = longest.value > 40
  chart.setOption({
    animation: false,
    grid: { left: 8, right: 16, top: 40, bottom: many ? 52 : 12, containLabel: true },
    legend: {
      top: 0, left: 0, textStyle: { color: text }, itemWidth: 18, itemHeight: 3,
      selected: Object.fromEntries(props.series.map((s) => [s.name, !s.hidden])),
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: cssVar('--surface'), borderColor: border, textStyle: { color: text },
      valueFormatter: (v) => (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%',
    },
    xAxis: {
      type: 'time', axisLine: { lineStyle: { color: border } },
      axisLabel: { color: muted, hideOverlap: true },
    },
    yAxis: {
      type: 'value', scale: true,
      axisLabel: { color: muted, formatter: (v) => v + '%' },
      splitLine: { lineStyle: { color: border } },
    },
    dataZoom: many ? [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }] : [],
    series: props.series.map((s) => {
      const color = cssVar(s.color) || cssVar('--line-other')
      return {
        name: s.name, type: 'line', data: s.data, showSymbol: s.data.length < 30,
        lineStyle: { width: s.dashed ? 1.75 : 2.5, color, type: s.dashed ? 'dashed' : 'solid' },
        itemStyle: { color },
      }
    }),
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
watch(() => props.series, render, { deep: true, flush: 'post' })
</script>

<template>
  <div v-if="longest < 2" class="empty">
    需要至少两天的数据才能画收益曲线（目前 {{ longest }} 天）<br />
    <small>The chart appears after at least 2 trading days.</small>
  </div>
  <div v-else ref="el" class="chart" role="img" :aria-label="label"></div>
</template>

<style scoped>
.chart { width: 100%; height: 320px; }
</style>
