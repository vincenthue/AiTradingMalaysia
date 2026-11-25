// 前端只用 anon / publishable key（只读，受 RLS 保护）。绝对不能放 service_role / secret key。
import { createClient } from '@supabase/supabase-js'

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

export async function loadAll() {
  const [account, positions, prices, trades, snapshots, runs, pending] = await Promise.all([
    q(supabase.from('account').select('*').eq('id', 1)),
    q(supabase.from('positions').select('*').order('ticker')),
    q(supabase.from('latest_prices').select('*')),
    q(supabase.from('trades').select('*').order('trade_date', { ascending: false }).order('id', { ascending: false }).limit(1000)),
    q(supabase.from('daily_snapshots').select('*').order('snapshot_date')),
    q(supabase.from('bot_runs').select('*').order('run_date', { ascending: false }).limit(10)),
    q(supabase.from('pending_orders').select('*').order('id')),
  ])
  return { account: account[0] ?? null, positions, prices, trades, snapshots, runs, pending }
}
