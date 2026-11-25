-- =====================================================================
-- AiTradingMalaysia 数据库结构（Supabase / PostgreSQL）— 多市场版（MY、US …）
-- 模拟交易｜虚拟资金。给「全新安装」用；可以重复执行（不会删除已有数据）。
--
-- 已经在用旧版（单一马股）的数据库：不要跑这个文件，改按顺序跑 sql/migrations/ 里的
-- 000（备份）→ 001 → 002 → 003，结果和这个文件完全一样。
--
-- 设计：
--   * 每张表都有 market 栏位（例如 'MY'、'US'），每个市场的数据完全分开；
--     市场代码只检查格式，以后加新市场不用改数据库。
--   * accounts 每个市场一行（主键 market）；bot 第一次跑新市场时自动建立。
--   * 所有表都开 RLS：anon / authenticated（前端，用 anon/publishable key）只能 SELECT；
--     写入只能用 service_role（bot，只放在 GitHub Secrets）。
--   * 交易用 PostgreSQL 函数（RPC）完成，函数内是单一 transaction：
--     改该市场现金、改持仓、写交易记录、删除挂单 → 一起成功或一起失败；
--     函数只碰 p_market 那个市场的数据（一个市场的现金不能买另一个市场的股票）。
-- =====================================================================

-- ---------------------------------------------------------------- 表

create table if not exists public.accounts (
    market       text primary key constraint accounts_market_check check (market ~ '^[A-Z]{2,5}$'),
    currency     text not null constraint accounts_currency_check check (currency ~ '^[A-Z]{3}$'),
    cash         numeric(14, 2) not null constraint accounts_cash_check check (cash >= 0),
    initial_cash numeric(14, 2) not null constraint accounts_initial_cash_check check (initial_cash > 0),
    updated_at   timestamptz not null default now()
);

create table if not exists public.positions (
    market     text not null constraint positions_market_check check (market ~ '^[A-Z]{2,5}$'),
    ticker     text not null,
    quantity   integer not null check (quantity > 0),
    avg_price  numeric(14, 4) not null check (avg_price > 0),
    buy_date   date not null,
    updated_at timestamptz not null default now(),
    constraint positions_pkey primary key (market, ticker)
);

create table if not exists public.trades (
    id         bigint generated always as identity primary key,
    market     text not null constraint trades_market_check check (market ~ '^[A-Z]{2,5}$'),
    trade_date date not null,
    ticker     text not null,
    side       text not null check (side in ('BUY', 'SELL')),
    quantity   integer not null check (quantity > 0),
    price      numeric(14, 4) not null check (price > 0),
    fees       numeric(14, 2) not null check (fees >= 0),
    reason     text not null default '',
    created_at timestamptz not null default now()
);
create index if not exists trades_trade_date_idx        on public.trades (trade_date desc);
create index if not exists trades_ticker_idx            on public.trades (ticker);
create index if not exists trades_market_trade_date_idx on public.trades (market, trade_date desc);

create table if not exists public.pending_orders (
    id           bigint generated always as identity primary key,
    market       text not null constraint pending_orders_market_check check (market ~ '^[A-Z]{2,5}$'),
    ticker       text not null,
    side         text not null check (side in ('BUY', 'SELL')),
    quantity     integer not null check (quantity > 0),
    reason       text not null default '',
    signal_date  date not null,
    signal_price numeric(14, 4) not null check (signal_price > 0),
    created_at   timestamptz not null default now()
);
create index if not exists pending_orders_market_idx on public.pending_orders (market);

create table if not exists public.daily_snapshots (
    market          text not null constraint daily_snapshots_market_check check (market ~ '^[A-Z]{2,5}$'),
    snapshot_date   date not null,
    total_value     numeric(14, 2) not null,
    cash            numeric(14, 2) not null,
    positions_value numeric(14, 2) not null,
    benchmark_close numeric(14, 4) not null,
    created_at      timestamptz not null default now(),
    constraint daily_snapshots_pkey primary key (market, snapshot_date)
);

create table if not exists public.bot_runs (
    id          bigint generated always as identity primary key,
    market      text not null constraint bot_runs_market_check check (market ~ '^[A-Z]{2,5}$'),
    run_date    date not null,                         -- 该市场当地的交易日
    status      text not null check (status in ('success', 'skipped', 'failed')),
    message     text not null default '',
    started_at  timestamptz not null,                  -- UTC
    finished_at timestamptz,
    constraint bot_runs_market_run_date_key unique (market, run_date)   -- 幂等：每个市场每天一笔
);

-- 每只股票 + 指数的最新收盘价（bot 每个交易日更新；dashboard 用来显示现价和盈亏）
create table if not exists public.latest_prices (
    market     text not null constraint latest_prices_market_check check (market ~ '^[A-Z]{2,5}$'),
    ticker     text not null,
    close      numeric(14, 4) not null check (close > 0),
    price_date date not null,
    updated_at timestamptz not null default now(),
    constraint latest_prices_pkey primary key (market, ticker)
);

-- ---------------------------------------------------------------- RLS

do $$
declare t text;
begin
    foreach t in array array['accounts','positions','trades','pending_orders','daily_snapshots','bot_runs','latest_prices']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists "read only for anon" on public.%I', t);
        execute format(
            'create policy "read only for anon" on public.%I for select to anon, authenticated using (true)', t);
        -- 双重保险：前端角色只有 SELECT 权限；写入只给 service_role
        execute format('revoke all on public.%I from anon, authenticated', t);
        execute format('grant select on public.%I to anon, authenticated', t);
        execute format('grant all on public.%I to service_role', t);
    end loop;
end $$;

grant usage on schema public to anon, authenticated, service_role;

-- ---------------------------------------------------------------- 交易函数（单一 transaction）

-- 买入：扣该市场现金、加该市场持仓（加权平均价）、写交易、删除挂单
create or replace function public.execute_buy(
    p_market text, p_ticker text, p_quantity integer, p_price numeric, p_fees numeric,
    p_trade_date date, p_reason text, p_order_id bigint default null
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
    p_market text, p_ticker text, p_quantity integer, p_price numeric, p_fees numeric,
    p_trade_date date, p_reason text, p_order_id bigint default null
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
    p_market text, p_run_date date, p_status text, p_message text,
    p_started_at timestamptz, p_finished_at timestamptz
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
revoke all on function public.execute_buy(text, text, integer, numeric, numeric, date, text, bigint)  from public, anon, authenticated;
revoke all on function public.execute_sell(text, text, integer, numeric, numeric, date, text, bigint) from public, anon, authenticated;
revoke all on function public.record_run(text, date, text, text, timestamptz, timestamptz)             from public, anon, authenticated;
grant execute on function public.execute_buy(text, text, integer, numeric, numeric, date, text, bigint)  to service_role;
grant execute on function public.execute_sell(text, text, integer, numeric, numeric, date, text, bigint) to service_role;
grant execute on function public.record_run(text, date, text, text, timestamptz, timestamptz)             to service_role;
