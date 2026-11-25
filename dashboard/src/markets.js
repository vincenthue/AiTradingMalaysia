// 市场显示用的名称 / 国旗 / 指数名称。
// 市场清单本身来自数据库的 accounts 表：markets.yaml 加了新市场、bot 跑过一次之后，
// dashboard 会自动多一个分页；这里没写到的市场也能显示（用代码和货币代码代替）。
const META = {
  MY: { flag: '🇲🇾', zh: '马股', en: 'Bursa Malaysia', index: 'FBM KLCI', color: '--line-my' },
  US: { flag: '🇺🇸', zh: '美股', en: 'US Stocks', index: 'S&P 500', color: '--line-us' },
}

const ORDER = ['MY', 'US']

export const CURRENCY_SYMBOL = { MYR: 'RM', USD: '$' }

export function marketMeta(code, currency) {
  const m = META[code] ?? { flag: '', zh: code, en: code, index: 'Index', color: '--line-other' }
  return { code, ...m, currency, symbol: CURRENCY_SYMBOL[currency] ?? currency ?? '' }
}

export const sortMarkets = (codes) =>
  [...codes].sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b))
