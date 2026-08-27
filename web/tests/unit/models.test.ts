import { describe, expect, it } from 'vitest'
import { frozenIdentityDigest } from '@/api/models'

describe('model rollout identity', () => {
  it('sorts the shard set before computing the exact sha256 digest', async () => {
    const expected = 'sha256:c30e5989ef490615c8d930766cad9112674359250ad9037f37b090777d05ff21'
    await expect(frozenIdentityDigest(['shard-b', 'shard-a'])).resolves.toBe(expected)
  })
})
