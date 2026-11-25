import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: { chunkSizeWarningLimit: 800 }, // ECharts 本身较大，已按需引入
})
