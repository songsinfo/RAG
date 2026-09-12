import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const client = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))
test('write requests carry session credentials, CSRF and a unique idempotency key', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return Response.json(url.endsWith('/csrf') ? { csrfToken: 'signed-token' } : { runId: 'run-1', status: 'QUEUED' }) }
  await client.api('/conversations/one/runs', 'POST', { question: 'test' })
  assert.equal(calls[0].url, '/api/v1/auth/csrf')
  assert.equal(calls[1].options.credentials, 'include')
  assert.equal(calls[1].options.headers['X-CSRF-Token'], 'signed-token')
  assert.ok(calls[1].options.headers['Idempotency-Key'])
  assert.equal(JSON.parse(calls[1].options.body).question, 'test')
})
test('HTTP failure is thrown with the service code and cannot be mistaken for a result', async () => {
  globalThis.fetch = async () => Response.json({ code: 'OPERATION_FORBIDDEN', message: '禁止访问' }, { status: 403 })
  await assert.rejects(client.api('/admin/jobs'), error => error.status === 403 && error.code === 'OPERATION_FORBIDDEN')
})
test('terminal business failures stop polling, independent of successful HTTP status', () => {
  assert.equal(client.terminal('RUNNING'), false)
  for (const state of ['FAILED', 'VERIFICATION_FAILED', 'CANCELLED', 'BUDGET_EXCEEDED', 'COMPLETED']) assert.equal(client.terminal(state), true)
})
