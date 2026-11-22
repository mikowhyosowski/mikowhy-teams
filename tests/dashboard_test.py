import pytest


@pytest.fixture
def dashboard_payload_with_data():
    return {
        'videos': [{'id': 'v1', 'title': 'News 1', 'status': 'ready'}],
        'queue': [{'id': 'j1', 'status': 'processing'}],
        'stats': {'generated_today': 3, 'failed': 0},
    }


@pytest.fixture
def dashboard_payload_empty():
    return {'videos': [], 'queue': [], 'stats': {'generated_today': 0, 'failed': 0}}


def test_dashboard_view_contains_all_key_sections(dashboard_service, dashboard_payload_with_data):
    view = dashboard_service.build_view(dashboard_payload_with_data)
    assert set(view.keys()) >= {'videos', 'queue', 'stats'}
    assert view['videos'] == dashboard_payload_with_data['videos']
    assert view['queue'] == dashboard_payload_with_data['queue']
    assert view['stats'] == dashboard_payload_with_data['stats']


def test_dashboard_empty_sections_are_empty_lists_not_null(dashboard_service, dashboard_payload_empty):
    view = dashboard_service.build_view(dashboard_payload_empty)
    assert view['videos'] == []
    assert view['queue'] == []
    assert view['stats'] == {'generated_today': 0, 'failed': 0}


def test_dashboard_render_includes_section_markers_even_when_empty(dashboard_service, dashboard_payload_empty):
    view = dashboard_service.build_view(dashboard_payload_empty)
    rendered = dashboard_service.render(view)
    for section in ('videos', 'queue', 'stats'):
        assert section in rendered, f'missing section in rendered dashboard: {section}'


def test_dashboard_regression_no_data_displayed_with_zero_values(dashboard_service):
    view = dashboard_service.build_view({'videos': [], 'queue': [], 'stats': {'generated_today': 0, 'failed': 0}})
    rendered = dashboard_service.render(view)
    assert rendered.strip() != '', 'dashboard rendered empty output despite valid payload'
    assert '0' in rendered, 'zero-valued stats must still be rendered, not swallowed by falsy checks'
