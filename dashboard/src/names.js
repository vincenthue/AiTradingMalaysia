// 显示用的股票名称（和 config/settings.yaml 的 watchlist 对应；没有的就只显示代码）
const NAMES = {
  '0166.KL': 'Inari',
  '4197.KL': 'Sime Darby',
  '3336.KL': 'IJM',
  '7113.KL': 'Top Glove',
  '6888.KL': 'Axiata',
  '7277.KL': 'Dialog',
  '3182.KL': 'Genting',
  '5296.KL': 'MR DIY',
  '^KLSE': 'FBM KLCI',
}

export const stockName = (ticker) => NAMES[ticker] ?? ''
