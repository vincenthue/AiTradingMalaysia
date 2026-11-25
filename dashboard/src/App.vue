<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import MarketView from './components/MarketView.vue'
import OverviewView from './components/OverviewView.vue'
import { configError, loadAll } from './supabase.js'
import { summarize } from './summary.js'
import { dateTime } from './format.js'

const raw = ref(null)
const error = ref(configError)
const loading = ref(false)
const loadedAt = ref(null)

async function refresh() {
  if (configError) return
  loading.value = true
  try {
    raw.value = await loadAll()
    loadedAt.value = new Date()
    error.value = null
  } catch (e) {
    error.value = `读取数据失败 Failed to load data：${e.message}`
  } finally {
    loading.value = false
  }
}

// 分页：#/overview、#/MY、#/US（网址可以直接分享 / 收藏）
const readHash = () => decodeURIComponent(location.hash.replace(/^#\/?/, '')) || 'overview'
const view = ref(readHash())
const onHash = () => { view.value = readHash() }
const go = (key) => { location.hash = `/${key}` }
onMounted(() => {
  refresh()
  window.addEventListener('hashchange', onHash)
})
onBeforeUnmount(() => window.removeEventListener('hashchange', onHash))

const markets = computed(() => (raw.value ?? []).map(summarize))
const tabs = computed(() => [
  { key: 'overview', label: '总览', en: 'Overview' },
  ...markets.value.map((m) => ({ key: m.code, label: `${m.meta.flag} ${m.meta.zh}`, en: m.code })),
])
const current = computed(() => markets.value.find((m) => m.code === view.value) ?? null)
const showOverview = computed(() => view.value === 'overview' || (raw.value && !current.value))
</script>

<template>
  <div class="ribbon" role="note">
    ⚠️ 模拟交易｜虚拟资金 · Paper trading with virtual money only — 不是真实交易 Not real trades
  </div>

  <div class="wrap">
    <header class="top">
      <div>
        <h1>AiTradingMalaysia</h1>
        <p class="muted sub">马股 + 美股模拟交易 Bursa Malaysia &amp; US paper trading · 50MA / 200MA</p>
      </div>
      <div class="top-actions">
        <span v-if="loadedAt" class="muted small">更新 Updated {{ dateTime(loadedAt) }}</span>
        <button type="button" :disabled="loading || !!configError" @click="refresh">
          {{ loading ? '读取中 Loading…' : '重新整理 Refresh' }}
        </button>
      </div>
    </header>

    <nav v-if="raw" class="tabs" aria-label="市场 Markets">
      <a
        v-for="t in tabs" :key="t.key" :href="`#/${t.key}`" class="tab"
        :class="{ active: (t.key === 'overview' && showOverview) || t.key === current?.code }"
        :aria-current="(t.key === 'overview' && showOverview) || t.key === current?.code ? 'page' : undefined"
      >
        {{ t.label }} <span class="tab-en">{{ t.en }}</span>
      </a>
    </nav>

    <div v-if="error" class="card alert" role="alert">{{ error }}</div>

    <template v-if="raw">
      <p v-if="!markets.length" class="card empty">还没有任何市场账户 No market accounts yet</p>
      <OverviewView v-else-if="showOverview" :markets="markets" @open="go" />
      <MarketView v-else :key="current.code" :m="current" />
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

.tabs {
  display: flex; gap: 4px; margin-top: 10px; border-bottom: 1px solid var(--border);
  overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch;
}
.tab {
  padding: 9px 14px; color: var(--muted); text-decoration: none; font-weight: 600; white-space: nowrap;
  border-bottom: 2px solid transparent; margin-bottom: -1px; border-radius: 8px 8px 0 0;
}
.tab:hover { color: var(--text); background: var(--surface-2); }
.tab.active { color: var(--text); border-bottom-color: var(--accent); }
.tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.tab-en { font-weight: 400; font-size: 12px; color: var(--muted); margin-left: 2px; }

.loading { padding: 40px 0; text-align: center; }
.foot { margin-top: 28px; text-align: center; }
</style>
