// 显示用的股票名称（和 config/settings.yaml 的 watchlist 对应；没有的就只显示代码）
const NAMES = {
  '1155.KL': 'Maybank',
  '1295.KL': 'Public Bank',
  '1023.KL': 'CIMB',
  '5347.KL': 'Tenaga Nasional',
  '6888.KL': 'Axiata',
  '^KLSE': 'FBM KLCI',
}

export const stockName = (ticker) => NAMES[ticker] ?? ''
