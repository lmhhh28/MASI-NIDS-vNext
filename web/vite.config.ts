import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const repoRoot = fileURLToPath(new URL('../', import.meta.url))

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: [
      {
        find: '@masi/control-api/client',
        replacement: fileURLToPath(
          new URL('../contracts/generated/typescript/control-api/client/index.ts', import.meta.url),
        ),
      },
      {
        find: '@masi/control-api',
        replacement: fileURLToPath(
          new URL('../contracts/generated/typescript/control-api/index.ts', import.meta.url),
        ),
      },
      { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
    ],
  },
  server: {
    fs: { allow: [repoRoot] },
    proxy: {
      '/api': { target: 'http://127.0.0.1:18080', changeOrigin: false },
      '/events': { target: 'http://127.0.0.1:18080', changeOrigin: false },
      '/oidc': { target: 'http://127.0.0.1:18080', changeOrigin: false },
    },
  },
  build: {
    target: 'es2022',
    manifest: true,
    cssCodeSplit: true,
    sourcemap: false,
    assetsInlineLimit: 2048,
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
        manualChunks(id) {
          if (id.includes('echarts')) return 'charts'
          if (id.includes('/node_modules/@tanstack/')) return 'query'
          if (id.includes('/node_modules/element-plus/') || id.includes('/node_modules/@element-plus/')) return 'ui'
          if (
            id.includes('/node_modules/vue/') ||
            id.includes('/node_modules/@vue/') ||
            id.includes('/node_modules/vue-router/') ||
            id.includes('/node_modules/pinia/')
          ) return 'vue-runtime'
          return undefined
        },
      },
    },
  },
})
