-- =====================================================================
-- AiTradingMalaysia 迁移 002：唯一约束改成按市场 + 建立美股账户
--
-- 什么时候执行：新版 bot（有 --market）已经合并到 main 之后、美股第一次运行之前。
--   旧版 bot 需要旧的单栏约束，所以不能早于合并；
--   美股和马股在同一天都要写 run_date / snapshot_date，所以必须早于美股第一次运行。
-- 请避开马股运行时段（马来西亚时间 18:15～18:45）。
--
-- 做什么：
--   * 主键 / 唯一约束改成 (market, …)：positions、daily_snapshots、latest_prices、bot_runs
--   * 删除旧的单栏约束（ticker、snapshot_date、run_date）
--   * 建立 US 账户：USD 200（已存在就不动）
--
-- 保留到迁移 003（第 6 步）：兼容 view「account」、market 默认值 'MY'（旧 dashboard 还在用）。
-- 整个文件是一个 transaction：任何一步出错，全部不生效。可以重复执行。
-- =====================================================================

begin;

do $$
begin
    -- positions：主键 ticker → (market, ticker)
    if exists (select 1 from pg_constraint where conname = 'positions_pkey'
               and conrelid = 'public.positions'::regclass and array_length(conkey, 1) = 1) then
        alter table public.positions drop constraint positions_pkey;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'positions_pkey') then
        alter table public.positions add constraint positions_pkey primary key (market, ticker);
    end if;
    alter table public.positions drop constraint if exists positions_market_ticker_key;

    -- daily_snapshots：主键 snapshot_date → (market, snapshot_date)
    if exists (select 1 from pg_constraint where conname = 'daily_snapshots_pkey'
               and conrelid = 'public.daily_snapshots'::regclass and array_length(conkey, 1) = 1) then
        alter table public.daily_snapshots drop constraint daily_snapshots_pkey;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'daily_snapshots_pkey') then
        alter table public.daily_snapshots
            add constraint daily_snapshots_pkey primary key (market, snapshot_date);
    end if;
    alter table public.daily_snapshots drop constraint if exists daily_snapshots_market_snapshot_date_key;

    -- latest_prices：主键 ticker → (market, ticker)
    if exists (select 1 from pg_constraint where conname = 'latest_prices_pkey'
               and conrelid = 'public.latest_prices'::regclass and array_length(conkey, 1) = 1) then
        alter table public.latest_prices drop constraint latest_prices_pkey;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'latest_prices_pkey') then
        alter table public.latest_prices add constraint latest_prices_pkey primary key (market, ticker);
    end if;
    alter table public.latest_prices drop constraint if exists latest_prices_market_ticker_key;
end $$;

-- bot_runs：删除旧的 run_date 唯一约束（保留 (market, run_date)）
alter table public.bot_runs drop constraint if exists bot_runs_run_date_key;

-- 美股账户（模拟交易｜虚拟资金 USD 200）
insert into public.accounts (market, currency, cash, initial_cash)
values ('US', 'USD', 200, 200)
on conflict (market) do nothing;

notify pgrst, 'reload schema';

commit;

-- ---------------------------------------------------------------- 核对结果
-- 1) 每个市场的账户
select market, currency, cash, initial_cash from public.accounts order by market;
