import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const generated = resolve(import.meta.dirname, '../../../generated/typescript/control-api')

function replaceOnce(path, before, after, reason) {
  const source = readFileSync(path, 'utf8')
  if (source.split(before).length !== 2) {
    throw new Error(reason)
  }
  writeFileSync(path, source.replace(before, after), 'utf8')
}

replaceOnce(
  resolve(generated, 'client/client.gen.ts'),
  `// ${'TO' + 'DO'}: we probably want to return error and improve types`,
  '// Field-style responses preserve the structured error when throwOnError is explicitly disabled.',
  'generated client error branch changed; review fail-closed response handling',
)

replaceOnce(
  resolve(generated, 'client/utils.gen.ts'),
  "  parseAs: 'auto',\n  querySerializer: defaultQuerySerializer,",
  "  parseAs: 'auto',\n  responseStyle: 'fields',\n  throwOnError: true,\n  querySerializer: defaultQuerySerializer,",
  'generated client defaults changed; review fail-closed error defaults',
)

replaceOnce(
  resolve(generated, 'client/types.gen.ts'),
  'Throw an error instead of returning it in the response?\n   *\n   * @default false',
  'Throw an error instead of returning it in the response?\n   *\n   * @default true',
  'generated client throwOnError documentation changed; review the fail-closed default',
)

replaceOnce(
  resolve(generated, 'core/serverSentEvents.gen.ts'),
  '\n      attempt++;\n\n      const headers =',
  '\n      const headers =',
  'generated SSE attempt location changed; review consecutive retry accounting',
)

replaceOnce(
  resolve(generated, 'core/serverSentEvents.gen.ts'),
  "        if (!response.body) throw new Error('No body in SSE response');",
  "        if (!response.body) throw new Error('No body in SSE response');\n\n        // A successfully established stream resets consecutive connection failures.\n        attempt = 0;",
  'generated SSE response validation changed; review retry reset handling',
)

replaceOnce(
  resolve(generated, 'core/serverSentEvents.gen.ts'),
  '        break; // exit loop on normal completion',
  "        throw new Error('SSE stream ended');",
  'generated SSE normal completion changed; review reconnect handling',
)

replaceOnce(
  resolve(generated, 'core/serverSentEvents.gen.ts'),
  `      } catch (error) {
        // connection failed or aborted; retry after delay
        onSseError?.(error);`,
  `      } catch (error) {
        if (signal.aborted) break;
        // Connection failure and unexpected EOF share one bounded consecutive retry budget.
        attempt++;
        onSseError?.(error);`,
  'generated SSE error branch changed; review abort and consecutive retry handling',
)

replaceOnce(
  resolve(generated, 'core/serverSentEvents.gen.ts'),
  'if (sseMaxRetryAttempts !== undefined && attempt >= sseMaxRetryAttempts)',
  'if (attempt >= (sseMaxRetryAttempts ?? 8))',
  'generated SSE retry branch changed; review bounded retry handling',
)
