import { describe, expect, it, vi, afterEach } from 'vitest';
import { DashboardApiError, fetchDashboard, parseDashboardResponse } from '../../src/dashboard/api';

function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('dashboard regression: null vs [] vs missing field', () => {
  it('accepts empty widgets array as a legal empty state', () => {
    const parsed = parseDashboardResponse({ widgets: [], generatedAt: '2026-11-22T15:57:00Z' });
    expect(parsed.widgets).toEqual([]);
  });
  it('rejects null instead of array', () => {
    expect(() => parseDashboardResponse({ widgets: null, generatedAt: '2026-11-22T15:57:00Z' }))
      .toThrowError(DashboardApiError);
  });
  it('rejects missing widgets field', () => {
    expect(() => parseDashboardResponse({ generatedAt: '2026-11-22T15:57:00Z' }))
      .toThrowError(DashboardApiError);
  });
  it('rejects widget missing required numeric value (seeded id)', () => {
    const rand = mulberry32(42);
    const badWidget = { id: `w-${Math.floor(rand() * 1000)}`, title: 'Missing value' };
    expect(() => parseDashboardResponse({ widgets: [badWidget], generatedAt: '2026-11-22T15:57:00Z' }))
      .toThrowError(DashboardApiError);
  });
  it('accepts trend=null per contract nullable semantics', () => {
    const parsed = parseDashboardResponse({
      widgets: [{ id: 'w1', title: 'Render', value: 3, trend: null, updatedAt: '2026-11-22T15:57:00Z' }],
      generatedAt: '2026-11-22T15:57:00Z',
    });
    expect(parsed.widgets[0].trend).toBeNull();
  });
  it('rejects non-ISO generatedAt', () => {
    expect(() => parseDashboardResponse({ widgets: [], generatedAt: '22-11-2026' }))
      .toThrowError(DashboardApiError);
  });
});

describe('fetchDashboard with deterministic mocked fetch', () => {
  it('maps HTTP 401 to DASHBOARD_UNAUTHORIZED', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    await expect(fetchDashboard(new AbortController().signal))
      .rejects.toMatchObject({ code: 'DASHBOARD_UNAUTHORIZED' });
  });
  it('maps HTTP 500 to DASHBOARD_FETCH_FAILED', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 500 })));
    await expect(fetchDashboard(new AbortController().signal))
      .rejects.toMatchObject({ code: 'DASHBOARD_FETCH_FAILED' });
  });
  it('parses empty widgets payload into ready data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ widgets: [], generatedAt: '2026-11-22T15:57:00Z' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )));
    const data = await fetchDashboard(new AbortController().signal);
    expect(data.widgets).toEqual([]);
    expect(data.generatedAt).toBe('2026-11-22T15:57:00Z');
  });
  it('rejects invalid JSON body as DASHBOARD_INVALID_PAYLOAD', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>not json</html>', { status: 200 })));
    await expect(fetchDashboard(new AbortController().signal))
      .rejects.toMatchObject({ code: 'DASHBOARD_INVALID_PAYLOAD' });
  });
});
