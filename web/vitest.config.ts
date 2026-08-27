import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

const aliases = [
  { find: '@masi/control-api/client', replacement: fileURLToPath(new URL('../contracts/generated/typescript/control-api/client/index.ts', import.meta.url)) },
  { find: '@masi/control-api', replacement: fileURLToPath(new URL('../contracts/generated/typescript/control-api/index.ts', import.meta.url)) },
  { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
]

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: aliases },
  test: {
    projects: [
      {
        resolve: { alias: aliases },
        test: {
          name: 'unit',
          environment: 'node',
          include: ['tests/unit/**/*.test.ts'],
          coverage: { enabled: false },
        },
      },
      {
        plugins: [vue()],
        resolve: { alias: aliases },
        test: {
          name: 'component',
          environment: 'jsdom',
          include: ['tests/component/**/*.test.ts'],
          setupFiles: ['tests/component/setup.ts'],
        },
      },
    ],
  },
})
