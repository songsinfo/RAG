export type Row = Record<string, any>
let csrf = ''
let csrfHeader = 'X-CSRF-Token'
export class ApiError extends Error { constructor(public status: number, public code: string, message: string) { super(message) } }
export async function refreshCsrf() {
  const response = await fetch('/api/v1/auth/csrf', { credentials: 'include', cache: 'no-store' })
  if (!response.ok) throw new ApiError(response.status, 'CSRF_UNAVAILABLE', '无法获取安全令牌，请检查后端连接')
  const value = await response.json()
  csrf = value.token || value.csrfToken
  csrfHeader = value.headerName || 'X-CSRF-Token'
  if (!csrf) throw new Error('后端未返回 CSRF token')
}
export async function api(path: string, method = 'GET', body?: unknown): Promise<any> {
  const headers: Record<string, string> = {}
  if (!['GET', 'HEAD'].includes(method)) {
    if (!csrf) await refreshCsrf()
    headers[csrfHeader] = csrf
    headers['Idempotency-Key'] = crypto.randomUUID()
  }
  if (body !== undefined && !(body instanceof FormData)) headers['Content-Type'] = 'application/json'
  const response = await fetch('/api/v1' + path, { method, credentials: 'include', cache: 'no-store', headers, body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body) })
  const text = await response.text()
  let data: any
  try { data = text ? JSON.parse(text) : {} } catch { throw new ApiError(response.status, 'INVALID_RESPONSE', '后端返回了无法解析的响应') }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('auth-expired'))
    throw new ApiError(response.status, data.code || 'HTTP_' + response.status, data.message || response.statusText)
  }
  return data
}
export const rows = (value: any): Row[] => Array.isArray(value) ? value : value?.items || value?.content || value?.results || []
export const pretty = (value: unknown) => JSON.stringify(value, null, 2)
export const keyOf = (row: Row) => row.id || row.documentId || row.runId || row.jobId || row.datasetId || row.versionId || row.conversationId || row.snapshotId || row.configId || row.caseId || row.resultId
export const terminal = (status: string) => !['QUEUED', 'RUNNING', 'PENDING'].includes(status)
