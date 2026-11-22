export interface DashboardWidgetData {
  id: string;
  title: string;
  value: number;
  trend: number | null;
  updatedAt: string;
}

export interface DashboardResponse {
  widgets: DashboardWidgetData[];
  generatedAt: string;
}

export type DashboardErrorCode =
  | 'DASHBOARD_FETCH_FAILED'
  | 'DASHBOARD_INVALID_PAYLOAD'
  | 'DASHBOARD_UNAUTHORIZED';

export type DashboardState =
  | { status: 'loading' }
  | { status: 'error'; code: DashboardErrorCode; message: string }
  | { status: 'ready'; data: DashboardResponse };
