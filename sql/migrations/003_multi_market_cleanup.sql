-- =====================================================================
-- AiTradingMalaysia 迁移 003：多市场「收尾」（contract）
--
-- 什么时候执行：新版 bot 和新版 dashboard（读 accounts、按 market 分开）都已上线之后。
--
-- 做什么：
--   * 删除兼容用的 view「account」（旧版 dashboard / 旧版 bot 才需要）
--   * 去掉所有表 market 栏位的默认值 'MY'：以后写入时必须明确写是哪个市场
--   * 交易函数的 p_market 改成必填（不再默认 'MY'），避免美股数据被误记成马股
--   * market / currency 的检查从「只能 MY、US」改成「格式正确的代码」，以后加新市场不用改数据库
--
-- 做完之后，数据库结构和全新安装的 sql/schema.sql 完全一样。
-- 整个文件是一个 transaction：任何一步出错，全部不生效。可以重复执行。
-- =====================================================================

begin;

drop view if exists public.account;

do $$
declare t text;
begin
    foreach t in array array['positions','trades','pending_orders','daily_snapshots','bot_runs','latest_prices']
    loop
        execute format('alter table public.%I alter column market drop default', t);
    end loop;
    -- 市场代码 / 货币代码只检查格式（不写死 MY、US）：以后加新市场不用改数据库
    foreach t in array array['accounts','positions','trades','pending_orders','daily_snapshots','bot_runs','latest_prices']
    loop
        execute format('alter table public.%I drop constraint if exists %I', t, t || '_market_check');
        execute format('alter table public.%I add constraint %I check (market ~ %L)',
                       t, t || '_market_check', '^[A-Z]{2,5}$');
    end loop;
    alter table public.accounts drop constraint if exists accounts_currency_check;
    alter table public.accounts add constraint accounts_currency_check check (currency ~ '^[A-Z]{3}$');
    -- 表是从 account 改名来的：检查约束也跟着改名，和全新安装一样
    if exists (select 1 from pg_constraint where conname = 'account_cash_check') then
        alter table public.accounts rename constraint account_cash_check to accounts_cash_check;
    end if;
    if exists (select 1 from pg_constraint where conname = 'account_initial_cash_check') then
        alter table public.accounts rename constraint account_initial_cash_check to accounts_initial_cash_check;
    end if;
end $$;

-- 交易函数：p_market 放第一个、必填（bot 用具名参数调用，顺序不影响）
drop function if exists public.execute_buy(text, integer, numeric, numeric, date, text, bigint, text);
drop function if exists public.execute_sell(text, integer, numeric, numeric, date, text, bigint, text);
drop function if exists public.record_run(date, text, text, timestamptz, timestamptz, text);

-- ↓↓↓ 以下函数和 sql/schema.sql 一模一样 ↓↓↓

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

notify pgrst, 'reload schema';

commit;

-- ---------------------------------------------------------------- 核对结果
-- 每个市场的账户（应该是 MY、US 两行）
select market, currency, cash, initial_cash from public.accounts order by market;
