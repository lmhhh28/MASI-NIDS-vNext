import axe from 'axe-core'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PageHeading from '@/components/PageHeading.vue'
import StatusMark from '@/components/StatusMark.vue'

describe('accessibility primitives', () => {
  it('has no automated WCAG A/AA violations in heading and non-color status semantics', async () => {
    const wrapper = mount({
      components: { PageHeading, StatusMark },
      template: '<main><PageHeading eyebrow="Evidence" title="Rule evidence" description="Independent facts." /><StatusMark value="hold" /></main>',
    }, { attachTo: document.body })
    const result = await axe.run(wrapper.element, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] } })
    expect(result.violations).toEqual([])
    wrapper.unmount()
  })
})
