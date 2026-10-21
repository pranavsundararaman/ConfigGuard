export type ConfigFormat = 'json' | 'yaml' | 'yml' | 'env'
export type Lifecycle = 'proposed' | 'deployed'

export interface ConfigVersion {
  id: string
  project_id: string
  configuration: Record<string, unknown>
  format: ConfigFormat
  lifecycle: Lifecycle
  created_at: string
  deployed_at?: string
}

export interface HistoricalResult {
  parameter: string
  status: 'not_assessed_non_numeric' | 'insufficient_history' | 'unusual' | 'within_historical_range'
  proposed_value: unknown
  observation_count: number
  required_observations?: number
  baseline_median?: number
  baseline_mad?: number
  robust_deviation?: number
  explanation: string
}

export interface AnalysisRecord {
  id: string
  project_id: string
  created_at?: string
  method?: string
  minimum_observations?: number
  results?: HistoricalResult[]
  review_recommended?: boolean
  limitations?: string | string[] | null
  baseline_version_ids?: string[]
  proposed_version_id?: string
  risk_score?: number
  classification?: string
  reasons?: string[]
  changes?: Array<Record<string, unknown>>
  [key: string]: unknown
}

const HISTORICAL_STATUSES = [
  'not_assessed_non_numeric',
  'insufficient_history',
  'unusual',
  'within_historical_range',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function normalizeLimitations(value: unknown): string[] {
  const values = typeof value === 'string' ? [value] : Array.isArray(value) ? value : []
  return values.filter((item): item is string => typeof item === 'string').map((item) => item.trim()).filter(Boolean)
}

export function normalizeStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

export function normalizeHistoricalResults(value: unknown): HistoricalResult[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((entry): HistoricalResult[] => {
    if (!isRecord(entry) || typeof entry.parameter !== 'string' || !HISTORICAL_STATUSES.includes(entry.status as typeof HISTORICAL_STATUSES[number])) return []
    return [{
      parameter: entry.parameter,
      status: entry.status as HistoricalResult['status'],
      proposed_value: entry.proposed_value,
      observation_count: typeof entry.observation_count === 'number' && Number.isFinite(entry.observation_count) ? entry.observation_count : 0,
      ...(typeof entry.required_observations === 'number' && Number.isFinite(entry.required_observations) ? { required_observations: entry.required_observations } : {}),
      ...(typeof entry.baseline_median === 'number' && Number.isFinite(entry.baseline_median) ? { baseline_median: entry.baseline_median } : {}),
      ...(typeof entry.baseline_mad === 'number' && Number.isFinite(entry.baseline_mad) ? { baseline_mad: entry.baseline_mad } : {}),
      ...(typeof entry.robust_deviation === 'number' && Number.isFinite(entry.robust_deviation) ? { robust_deviation: entry.robust_deviation } : {}),
      explanation: typeof entry.explanation === 'string' ? entry.explanation : '',
    }]
  })
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch {
    throw new Error('Cannot reach ConfigGuard API. Start the backend at http://localhost:8000 and try again.')
  }
  if (!response.ok) {
    let message = `Request failed (${response.status}).`
    try {
      const body = await response.json() as { detail?: string | Array<{ msg?: string }> }
      if (typeof body.detail === 'string') message = body.detail
      else if (Array.isArray(body.detail)) message = body.detail.map((item) => item.msg).filter(Boolean).join('; ') || message
    } catch { /* Keep the safe status message. */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

const projectPath = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}`

export const api = {
  health: () => request<{ status: string; service: string }>('/api/health'),
  versions: (projectId: string) => request<ConfigVersion[]>(`${projectPath(projectId)}/config-versions`),
  analyses: (projectId: string) => request<AnalysisRecord[]>(`${projectPath(projectId)}/analyses`),
  analysis: (projectId: string, id: string) => request<AnalysisRecord>(`${projectPath(projectId)}/analyses/${encodeURIComponent(id)}`),
  saveVersion: (projectId: string, configuration: Record<string, unknown>, format: ConfigFormat) =>
    request<ConfigVersion>(`${projectPath(projectId)}/config-versions`, { method: 'POST', body: JSON.stringify({ configuration, format }) }),
  deployVersion: (projectId: string, id: string) =>
    request<ConfigVersion>(`${projectPath(projectId)}/config-versions/${encodeURIComponent(id)}/deploy`, { method: 'POST' }),
  analyze: (projectId: string, configuration: Record<string, unknown>, format: ConfigFormat, min_observations: number) =>
    request<AnalysisRecord>(`${projectPath(projectId)}/historical-analysis`, {
      method: 'POST', body: JSON.stringify({ configuration, format, min_observations }),
    }),
}
