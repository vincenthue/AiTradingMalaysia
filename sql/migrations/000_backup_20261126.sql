-- =====================================================================
-- AiTradingMalaysia：升级多市场前的数据库内备份（只复制，不修改任何原有数据）
-- 在 Supabase SQL Editor 执行。可以重复执行（已存在的备份表不会被覆盖）。
--
-- 备份放在独立的 schema「backup_20261126」，前端 API 读不到（只公开 public）。
-- 迁移出问题时优先用 001_rollback.sql（不需要动备份）；备份是最后一道保险。
-- =====================================================================

create schema if not exists backup_20261126;
revoke all on schema backup_20261126 from public, anon, authenticated;

create table if not exists backup_20261126.account         as table public.account;
create table if not exists backup_20261126.positions       as table public.positions;
create table if not exists backup_20261126.trades          as table public.trades;
create table if not exists backup_20261126.pending_orders  as table public.pending_orders;
create table if not exists backup_20261126.daily_snapshots as table public.daily_snapshots;
create table if not exists backup_20261126.bot_runs        as table public.bot_runs;
create table if not exists backup_20261126.latest_prices   as table public.latest_prices;

-- 核对：原表和备份的行数必须一样（每一行的 same 都应该是 true）
select t.name,
       t.original,
       t.backup,
       t.original = t.backup as same
from (
    select 'account' as name,
           (select count(*) from public.account) as original,
           (select count(*) from backup_20261126.account) as backup
    union all select 'positions',
           (select count(*) from public.positions), (select count(*) from backup_20261126.positions)
    union all select 'trades',
           (select count(*) from public.trades), (select count(*) from backup_20261126.trades)
    union all select 'pending_orders',
           (select count(*) from public.pending_orders), (select count(*) from backup_20261126.pending_orders)
    union all select 'daily_snapshots',
           (select count(*) from public.daily_snapshots), (select count(*) from backup_20261126.daily_snapshots)
    union all select 'bot_runs',
           (select count(*) from public.bot_runs), (select count(*) from backup_20261126.bot_runs)
    union all select 'latest_prices',
           (select count(*) from public.latest_prices), (select count(*) from backup_20261126.latest_prices)
) t;
