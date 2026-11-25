// 前端只用 anon / publishable key（只读，受 RLS 保护）。绝对不能放 service_role / secret key。
import { createClient } from '@supabase/supabase-js'
import { marketMeta, sortMarkets } from './markets.js'

const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_ANON_KEY

export const configError =
  !url || !key
    ? '缺少 VITE_SUPABASE_URL 或 VITE_SUPABASE_ANON_KEY（Missing environment variables）'
    : key.startsWith('sb_secret_')
      ? '前端不能使用 secret key，请改用 anon / publishable key（Never use the secret key in the browser）'
      : null

export const supabase = configError
  ? null
  : createClient(url, key, { auth: { persistSession: false } })

async function q(builder) {
  const { data, error } = await builder
  if (error) throw new Error(error.message)
  return data ?? []
}

const byMarket = (rows, code) => rows.filter((r) => r.market === code)

// 一次读所有市场，再按 market 分组；每个市场的数据完全分开显示
export async function loadAll() {
  const [accounts, positions, prices, trades, snapshots, runs, pending] = await Promise.all([
    q(supabase.from('accounts').select('*')),
    q(supabase.from('positions').select('*').order('ticker')),
    q(supabase.from('latest_prices').select('*')),
    q(supabase.from('trades').select('*').order('trade_date', { ascending: false }).order('id', { ascending: false }).limit(2000)),
    q(supabase.from('daily_snapshots').select('*').order('snapshot_date')),
    q(supabase.from('bot_runs').select('*').order('run_date', { ascending: false }).order('id', { ascending: false }).limit(200)),
    q(supabase.from('pending_orders').select('*').order('id')),
  ])
  return sortMarkets(accounts.map((a) => a.market)).map((code) => {
    const account = accounts.find((a) => a.market === code)
    return {
      code,
      meta: marketMeta(code, account.currency),
      account,
      positions: byMarket(positions, code),
      prices: byMarket(prices, code),
      trades: byMarket(trades, code),
      snapshots: byMarket(snapshots, code),
      runs: byMarket(runs, code).slice(0, 10),
      pending: byMarket(pending, code),
    }
  })
}
