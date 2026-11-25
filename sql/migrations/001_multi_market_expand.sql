-- =====================================================================
-- AiTradingMalaysia 迁移 001：多市场「扩充」（expand）
--
-- 先执行 000_backup_20261126.sql 备份，再执行本文件。
-- 请避开马股运行时段（马来西亚时间 18:15～18:45）。
--
-- 做什么：
--   * account（单行）→ accounts（每个市场一行，主键 market），现有那一行 = 'MY' / 'MYR'
--   * 其他 6 张表加 market 栏位（只能是 'MY' 或 'US'），现有数据全部 = 'MY'
--   * 新增按市场的唯一约束：(market, run_date)、(market, snapshot_date)、(market, ticker)
--   * 交易函数加参数 p_market（默认 'MY'），每个市场只动自己的现金和持仓
--
-- 兼容（让正在运行的旧马股 bot 和旧 dashboard 不坏）：
--   * 保留一个只读 view「account」（= accounts 里 MY 那一行，id 固定是 1）
--   * 保留旧的单栏唯一约束（ticker、snapshot_date、run_date）
--   * market 栏位暂时默认 'MY'
--   之后分两次收尾：
--     迁移 002（第 4 步，新 bot 上线后、美股第一次运行前）：删除旧的单栏唯一约束
--       （否则美股和马股同一天的 run_date / snapshot_date 会互相冲突）
--     迁移 003（第 6 步，新 dashboard 上线后）：删除兼容 view、去掉 market 默认值
--
-- 出问题想退回：执行 001_rollback.sql（保留所有数据），再执行 main 分支的 sql/schema.sql。
--
-- 整个文件是一个 transaction：任何一步出错，全部不生效。可以重复执行。
-- =====================================================================

begin;

-- ---------------------------------------------------------------- 1. account → accounts

do $$
begin
    if to_regclass('public.accounts') is null then
        alter table public.account rename to accounts;
    end if;
end $$;

alter table public.accounts add column if not exists market   text;
alter table public.accounts add column if not exists currency text;
update public.accounts set market   = 'MY'  where market   is null;
update public.accounts set currency = 'MYR' where currency is null;
alter table public.accounts alter column market   set not null;
alter table public.accounts alter column currency set not null;

-- 删除旧的 id 栏位（连同 id 主键和 id = 1 检查），改用 market 当主键
alter table public.accounts drop column if exists id;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'accounts_pkey') then
        alter table public.accounts add constraint accounts_pkey primary key (market);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'accounts_market_check') then
        alter table public.accounts add constraint accounts_market_check check (market in ('MY', 'US'));
    end if;
    if not exists (select 1 from pg_constraint where conname = 'accounts_currency_check') then
        alter table public.accounts add constraint accounts_currency_check check (currency in ('MYR', 'USD'));
    end if;
end $$;

-- 兼容 view：旧 bot / 旧 dashboard 读 account?id=eq.1 时，看到的是 MY 账户
-- security_invoker：用查询者自己的权限（anon 照样只能读）
create or replace view public.account with (security_invoker = true) as
    select 1::smallint as id, cash, initial_cash, updated_at
      from public.accounts
     where market = 'MY';

revoke all on public.account from anon, authenticated;
grant select on public.account to anon, authenticated;
grant all on public.account to service_role;

-- ---------------------------------------------------------------- 2. 其他表加 market

do $$
declare t text;
begin
    foreach t in array array['positions','trades','pending_orders','daily_snapshots','bot_runs','latest_prices']
    loop
        -- not null + default：现有的每一行都自动填 'MY'
        execute format('alter table public.%I add column if not exists market text not null default %L', t, 'MY');
        if not exists (select 1 from pg_constraint where conname = t || '_market_check') then
            execute format('alter table public.%I add constraint %I check (market in (%L, %L))',
                           t, t || '_market_check', 'MY', 'US');
        end if;
    end loop;
end $$;

-- ---------------------------------------------------------------- 3. 按市场的唯一约束 + 索引

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'positions_market_ticker_key') then
        alter table public.positions add constraint positions_market_ticker_key unique (market, ticker);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'daily_snapshots_market_snapshot_date_key') then
        alter table public.daily_snapshots
            add constraint daily_snapshots_market_snapshot_date_key unique (market, snapshot_date);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'bot_runs_market_run_date_key') then
        alter table public.bot_runs add constraint bot_runs_market_run_date_key unique (market, run_date);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'latest_prices_market_ticker_key') then
        alter table public.latest_prices add constraint latest_prices_market_ticker_key unique (market, ticker);
    end if;
end $$;

create index if not exists trades_market_trade_date_idx on public.trades (market, trade_date desc);
create index if not exists pending_orders_market_idx    on public.pending_orders (market);

-- ---------------------------------------------------------------- 4. 交易函数（加 p_market）

-- 先删旧版本（参数不同，不删的话会变成两个同名函数，API 会分不清）
drop function if exists public.execute_buy(text, integer, numeric, numeric, date, text, bigint);
drop function if exists public.execute_sell(text, integer, numeric, numeric, date, text, bigint);
drop function if exists public.record_run(date, text, text, timestamptz, timestamptz);

-- 买入：扣该市场现金、加该市场持仓（加权平均价）、写交易、删除挂单
create or replace function public.execute_buy(
    p_ticker text, p_quantity integer, p_price numeric, p_fees numeric,
    p_trade_date date, p_reason text, p_order_id bigint default null,
    p_market text default 'MY'
) returns public.trades
language plpgsql
set search_path = public
as $$
declare
    v_cash  numeric;
    v_cost  numeric := round(p_quantity * p_price + p_fees, 2);
    v_trade public.trades;
begin
    if p_quantity <= 0 or p_price <= 0 or p_fees < 0 then
        raise exception 'invalid buy parameters';
    end if;
    select cash into v_cash from accounts where market = p_market for update;
    if v_cash is null then
        raise exception 'account for market % not initialised', p_market;
    end if;
    if v_cash - v_cost < 0 then
        raise exception 'cash would become negative (market %, cash %, cost %)', p_market, v_cash, v_cost;
    end if;

    update accounts set cash = round(v_cash - v_cost, 2), updated_at = now() where market = p_market;

    insert into positions as p (market, ticker, quantity, avg_price, buy_date)
    values (p_market, p_ticker, p_quantity, round(p_price, 4), p_trade_date)
    on conflict (market, ticker) do update
        set avg_price  = round((p.avg_price * p.quantity + excluded.avg_price * excluded.quantity)
                               / (p.quantity + excluded.quantity), 4),
            quantity   = p.quantity + excluded.quantity,
            updated_at = now();

    insert into trades (market, trade_date, ticker, side, quantity, price, fees, reason)
    values (p_market, p_trade_date, p_ticker, 'BUY', p_quantity, p_price, p_fees, p_reason)
    returning * into v_trade;

    if p_order_id is not null then
        delete from pending_orders where id = p_order_id and market = p_market;
        if not found then
            raise exception 'pending order % not found in market %', p_order_id, p_market;
        end if;
    end if;
    return v_trade;
end $$;

-- 卖出：加该市场现金、减该市场持仓（卖光就删除）、写交易、删除挂单
create or replace function public.execute_sell(
    p_ticker text, p_quantity integer, p_price numeric, p_fees numeric,
    p_trade_date date, p_reason text, p_order_id bigint default null,
    p_market text default 'MY'
) returns public.trades
language plpgsql
set search_path = public
as $$
declare
    v_held  integer;
    v_trade public.trades;
begin
    if p_quantity <= 0 or p_price <= 0 or p_fees < 0 then
        raise exception 'invalid sell parameters';
    end if;
    perform 1 from accounts where market = p_market for update;
    if not found then
        raise exception 'account for market % not initialised', p_market;
    end if;
    select quantity into v_held from positions
     where market = p_market and ticker = p_ticker for update;
    if v_held is null then
        raise exception 'no position in % for market % (short selling not allowed)', p_ticker, p_market;
    end if;
    if p_quantity > v_held then
        raise exception 'sell % > held % for %', p_quantity, v_held, p_ticker;
    end if;

    update accounts
       set cash = round(cash + p_quantity * p_price - p_fees, 2), updated_at = now()
     where market = p_market;

    if p_quantity = v_held then
        delete from positions where market = p_market and ticker = p_ticker;
    else
        update positions set quantity = quantity - p_quantity, updated_at = now()
         where market = p_market and ticker = p_ticker;
    end if;

    insert into trades (market, trade_date, ticker, side, quantity, price, fees, reason)
    values (p_market, p_trade_date, p_ticker, 'SELL', p_quantity, p_price, p_fees, p_reason)
    returning * into v_trade;

    if p_order_id is not null then
        delete from pending_orders where id = p_order_id and market = p_market;
        if not found then
            raise exception 'pending order % not found in market %', p_order_id, p_market;
        end if;
    end if;
    return v_trade;
end $$;

-- 运行记录：同一市场同一天已经 success 的不覆盖；failed / skipped 会被新结果覆盖
create or replace function public.record_run(
    p_run_date date, p_status text, p_message text,
    p_started_at timestamptz, p_finished_at timestamptz,
    p_market text default 'MY'
) returns void
language sql
set search_path = public
as $$
    insert into bot_runs (market, run_date, status, message, started_at, finished_at)
    values (p_market, p_run_date, p_status, p_message, p_started_at, p_finished_at)
    on conflict (market, run_date) do update
        set status = excluded.status, message = excluded.message,
            started_at = excluded.started_at, finished_at = excluded.finished_at
        where bot_runs.status <> 'success';
$$;

-- 函数只给 bot（service_role）用
revoke all on function public.execute_buy(text, integer, numeric, numeric, date, text, bigint, text)  from public, anon, authenticated;
revoke all on function public.execute_sell(text, integer, numeric, numeric, date, text, bigint, text) from public, anon, authenticated;
revoke all on function public.record_run(date, text, text, timestamptz, timestamptz, text)             from public, anon, authenticated;
grant execute on function public.execute_buy(text, integer, numeric, numeric, date, text, bigint, text)  to service_role;
grant execute on function public.execute_sell(text, integer, numeric, numeric, date, text, bigint, text) to service_role;
grant execute on function public.record_run(date, text, text, timestamptz, timestamptz, text)             to service_role;

-- 让 Supabase API 立刻看到新结构
notify pgrst, 'reload schema';

commit;

-- ---------------------------------------------------------------- 核对结果
select 'accounts' as tbl, market, count(*) as rows from public.accounts group by market
union all select 'positions',       market, count(*) from public.positions       group by market
union all select 'trades',          market, count(*) from public.trades          group by market
union all select 'pending_orders',  market, count(*) from public.pending_orders  group by market
union all select 'daily_snapshots', market, count(*) from public.daily_snapshots group by market
union all select 'bot_runs',        market, count(*) from public.bot_runs        group by market
union all select 'latest_prices',   market, count(*) from public.latest_prices   group by market
order by 1, 2;
