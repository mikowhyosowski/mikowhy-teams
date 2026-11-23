import subprocess
import re

REPO = '/home/miko/Projekty/M+/mikowhy-teams'


def _git(*args: str) -> str:
    # Instrumentacja git przez subprocess; worker LLM nie wywoluje git bezposrednio.
    result = subprocess.run(
        ['git', *args], cwd=REPO, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_staging_branch_exists() -> None:
    branches = _git('branch', '--list', 'staging')
    assert 'staging' in branches, 'brak lokalnej galezi staging'


def test_staging_pushed_to_remote() -> None:
    _git('fetch', '--quiet')
    remote = _git('branch', '-r', '--list', 'origin/staging')
    assert remote, 'brak origin/staging na remote'
    assert _git('rev-parse', 'staging') == _git('rev-parse', 'origin/staging')


def test_merge_hook_configured() -> None:
    # Zmiana konfiguracyjna: silnik musi domergowywac do staging przed push.
    with open(f'{REPO}/engine/git_guard.py', encoding='utf-8') as fh:
        source = fh.read()
    assert 'staging' in source, 'git_guard.py nie zawiera logiki merge do staging'


def test_staging_commits_are_conventional() -> None:
    log = _git('log', 'origin/staging', '--pretty=format:%s')
    pattern = re.compile(r'^(feat|fix|chore)(\([a-z0-9-]+\))?: .{1,72}$', re.IGNORECASE)
    for subject in log.splitlines():
        assert pattern.match(subject), f'commit nieconwencjonalny: {subject}'
