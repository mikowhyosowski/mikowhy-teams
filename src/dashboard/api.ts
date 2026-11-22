import type { DashboardErrorCode, DashboardResponse, DashboardWidgetData } from './types';

const DASHBOARD_ENDPOINT = '/api/v1/dashboard';

export class DashboardApiError extends Error {
  readonly code: DashboardErrorCode;
  constructor(code: DashboardErrorCode, message: string) {
    super(message);
    this.name = 'DashboardApiError';
    this.code = code;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isIsoDateTime(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && !Number.isNaN(Date.parse(value));
}

function parseWidget(raw: unknown): DashboardWidgetData | null {
  if (!isRecord(raw)) return null;
  const { id, title, value, trend, updatedAt } = raw;
  if (typeof id !== 'string' || id.length === 0) return null;
  if (typeof title !== 'string' || title.length === 0) return null;
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (trend !== null && (typeof trend !== 'number' || !Number.isFinite(trend))) return null;
  if (!isIsoDateTime(updatedAt)) return null;
  return { id, title, value, trend, updatedAt };
}

export function parseDashboardResponse(raw: unknown): DashboardResponse {
  if (!isRecord(raw)) {
    throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Dashboard payload is not an object');
  }
  if (!('widgets' in raw)) {
    throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Missing required field: widgets');
  }
  if (raw.widgets === null) {
    throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Field widgets must be an array, got null');
  }
  if (!Array.isArray(raw.widgets)) {
    throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Field widgets must be an array');
  }
  if (!isIsoDateTime(raw.generatedAt)) {
    throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Field generatedAt must be an ISO-8601 date-time');
  }
  const widgets: DashboardWidgetData[] = [];
  for (const item of raw.widgets) {
    const parsed = parseWidget(item);
    if (parsed === null) {
      throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Widget entry failed schema validation');
    }
    widgets.push(parsed);
  }
  return { widgets, generatedAt: raw.generatedAt };
}

export async function fetchDashboard(signal: AbortSignal): Promise<DashboardResponse> {
  let response: Response;
  try {
    response = await fetch(DASHBOARD_ENDPOINT, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new DashboardApiError('DASHBOARD_FETCH_FAILED', 'Network request to dashboard endpoint failed');
  }
  if (response.status === 401 || response.status === 403) {
    throw new DashboardApiError('DASHBOARD_UNAUTHORIZED', `Dashboard endpoint returned HTTP ${response.status}`);
  }
  if (!response.ok) {
    throw new DashboardApiError('DASHBOARD_FETCH_FAILED', `Dashboard endpoint returned HTTP ${response.status}`);
  }
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new DashboardApiError('DASHBOARD_INVALID_PAYLOAD', 'Dashboard response is not valid JSON');
  }
  return parseDashboardResponse(raw);
}
