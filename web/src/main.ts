import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import App from './App.vue'
import { router } from './router'
import { queryCacheMetrics, queryClient } from './query'
import { runtimeResourceMetrics } from './runtime-metrics'
import './styles/base.css'
import 'element-plus/theme-chalk/base.css'
import 'element-plus/theme-chalk/el-button.css'
import 'element-plus/theme-chalk/el-tooltip.css'
import 'element-plus/theme-chalk/el-dialog.css'
import 'element-plus/theme-chalk/el-drawer.css'
import 'element-plus/theme-chalk/el-form.css'
import 'element-plus/theme-chalk/el-form-item.css'
import 'element-plus/theme-chalk/el-input.css'
import 'element-plus/theme-chalk/el-select.css'
import 'element-plus/theme-chalk/el-table.css'
import 'element-plus/theme-chalk/el-pagination.css'
import 'element-plus/theme-chalk/el-tabs.css'
import 'element-plus/theme-chalk/el-tag.css'
import 'element-plus/theme-chalk/el-alert.css'

Object.defineProperty(window, '__masiRuntimeReadback', {
  configurable: false,
  enumerable: false,
  writable: false,
  value: () => ({ ...queryCacheMetrics(), ...runtimeResourceMetrics() }),
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(VueQueryPlugin, { queryClient })
app.mount('#app')
