import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'

type Theme = 'light' | 'dark'
type Density = 'compact' | 'comfortable'

function readPreference<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  const value = window.localStorage.getItem(key)
  return allowed.includes(value as T) ? (value as T) : fallback
}

export const useUiStore = defineStore('ui', () => {
  const theme = ref<Theme>(readPreference('masi.theme', ['light', 'dark'], 'light'))
  const density = ref<Density>(readPreference('masi.density', ['compact', 'comfortable'], 'compact'))
  const sidebarCollapsed = ref(window.localStorage.getItem('masi.sidebar-collapsed') === 'true')
  const mobileNavigationOpen = ref(false)

  const dangerousViewportSupported = computed(() => window.matchMedia('(min-width: 1024px)').matches)

  function applyToDocument(): void {
    document.documentElement.dataset.theme = theme.value
    document.documentElement.dataset.density = density.value
  }

  function toggleTheme(): void {
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }

  function toggleDensity(): void {
    density.value = density.value === 'compact' ? 'comfortable' : 'compact'
  }

  watch(theme, (value) => {
    window.localStorage.setItem('masi.theme', value)
    applyToDocument()
  })
  watch(density, (value) => {
    window.localStorage.setItem('masi.density', value)
    applyToDocument()
  })
  watch(sidebarCollapsed, (value) => window.localStorage.setItem('masi.sidebar-collapsed', String(value)))

  applyToDocument()

  return {
    theme,
    density,
    sidebarCollapsed,
    mobileNavigationOpen,
    dangerousViewportSupported,
    toggleTheme,
    toggleDensity,
    applyToDocument,
  }
})
