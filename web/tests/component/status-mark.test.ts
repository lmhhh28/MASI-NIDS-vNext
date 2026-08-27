import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import StatusMark from '@/components/StatusMark.vue'

describe('StatusMark', () => {
  it('uses text and a non-color shape for HOLD', () => {
    const wrapper = mount(StatusMark, { props: { value: 'hold' } })
    expect(wrapper.text()).toContain('hold')
    expect(wrapper.find('.status-mark--hold').exists()).toBe(true)
    expect(wrapper.find('.status-mark__shape').attributes('aria-hidden')).toBe('true')
  })
})
