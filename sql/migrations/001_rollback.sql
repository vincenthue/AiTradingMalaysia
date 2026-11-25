-- =====================================================================
-- AiTradingMalaysia：退回迁移 001（回到单一马股结构，保留所有马股数据）
--
-- 只在迁移 001 之后、还没有美股数据时使用（有 US 数据会直接报错，不做任何改动）。
-- 执行完本文件后，再执行一次 main 分支的 sql/schema.sql（恢复旧版交易函数和权限）。
-- 整个文件是一个 transaction：任何一步出错，全部不生效。
-- =====================================================================

begin;

do $$
begin
    if exists (select 1 from public.accounts where market <> 'MY')
       or exists (select 1 from public.trades where market <> 'MY')
       or exists (select 1 from public.positions where market <> 'MY')
       or exists (select 1 from public.bot_runs where market <> 'MY') then
        raise exception '已经有非 MY 的数据，不能自动退回；请联系开发者处理';
    end if;
end $$;

drop function if exists public.execute_buy(text, integer, numeric, numeric, date, text, bigint, text);
drop function if exists public.execute_sell(text, integer, numeric, numeric, date, text, bigint, text);
drop function if exists public.record_run(date, text, text, timestamptz, timestamptz, text);

drop view if exists public.account;

-- accounts → account（id = 1 单行）
alter table public.accounts drop constraint if exists accounts_pkey;
alter table public.accounts add column if not exists id smallint not null default 1 check (id = 1);
alter table public.accounts add constraint account_pkey primary key (id);
alter table public.accounts drop column market;
alter table public.accounts drop column currency;
alter table public.accounts rename to account;

drop index if exists public.trades_market_trade_date_idx;
drop index if exists public.pending_orders_market_idx;

do $$
declare t text;
begin
    foreach t in array array['positions','trades','pending_orders','daily_snapshots','bot_runs','latest_prices']
    loop
        -- 删除 market 栏位会连带删除包含它的约束（market 检查、(market, …) 唯一约束）
        execute format('alter table public.%I drop column if exists market', t);
    end loop;
end $$;

notify pgrst, 'reload schema';

commit;
