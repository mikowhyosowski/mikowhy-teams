import { useDashboardData } from './useDashboardData';

export function Dashboard(): JSX.Element {
  const { state, refresh } = useDashboardData();

  if (state.status === 'loading') {
    return <div role="status" aria-live="polite">Loading dashboard...</div>;
  }
  if (state.status === 'error') {
    return (
      <div role="alert">
        <p>Dashboard could not be loaded ({state.code}).</p>
        <button type="button" onClick={refresh}>Retry</button>
      </div>
    );
  }
  const { widgets, generatedAt } = state.data;
  return (
    <main aria-label="Dashboard">
      <header>
        <h1>Dashboard</h1>
        <time dateTime={generatedAt}>Updated: {generatedAt}</time>
      </header>
      {widgets.length === 0 ? (
        <p>No data to display.</p>
      ) : (
        <ul>
          {widgets.map((widget) => (
            <li key={widget.id}>
              <h2>{widget.title}</h2>
              <data value={widget.value}>{widget.value}</data>
              {widget.trend !== null && (
                <span>{widget.trend >= 0 ? '+' : ''}{widget.trend}%</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
