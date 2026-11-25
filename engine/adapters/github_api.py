"""GitHubApiAdapter — stub VcsWorkspacePort dla GitHub API (bez worktree)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.ports import WorkspaceHandle


class GitHubApiAdapter:
    """
    Adapter VcsWorkspacePort używający GitHub REST/GraphQL API.
    
    Zamiast worktree lokalnego, tworzy branch na GitHub i operuje na nim przez API.
    Przydatne w CI/CD gdzie nie ma dostępu do filesystemu.
    
    Wymaga: GITHUB_TOKEN z uprawnieniami repo:read, repo:write
    Konfiguracja przez env vars:
    - GITHUB_OWNER: organizacja/użytkownik
    - GITHUB_REPO: nazwa repozytorium
    - GITHUB_BASE_BRANCH: gałąź bazowa (domyślnie: main)
    """

    def __init__(
        self,
        owner: str | None = None,
        repo: str | None = None,
        base_branch: str = "main",
    ):
        import os
        self.owner = owner or os.environ.get("GITHUB_OWNER")
        self.repo = repo or os.environ.get("GITHUB_REPO")
        self.base_branch = base_branch or os.environ.get("GITHUB_BASE_BRANCH", "main")
        self._client = None  # httpx.AsyncClient

    def _get_client(self):
        if self._client is None:
            import httpx
            import os
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise RuntimeError("GITHUB_TOKEN not set")
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "mikowhy-teams"},
                timeout=30.0,
            )
        return self._client

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        client = self._get_client()
        resp = await client.post("/graphql", json={"query": query, "variables": variables or {}})
        resp.raise_for_status()
        return resp.json()

    async def _rest(self, method: str, path: str, **kwargs) -> dict:
        client = self._get_client()
        resp = await client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def prepare_workspace(self, task: dict[str, Any]) -> WorkspaceHandle:
        # STUB: W rzeczywistości tworzy branch na GitHub przez API
        branch = task.get("branch") or f"task/{task['id']}"
        return WorkspaceHandle(
            task_id=task["id"],
            path=f"github://{self.owner}/{self.repo}/{branch}",
            branch=branch,
            base=self.base_branch,
        )

    def verify_scope(self, workspace: WorkspaceHandle, scope_paths: list[str]) -> list[str]:
        # STUB: Sprawdza zmienione pliki przez GitHub API
        return []

    def commit_and_push(self, workspace: WorkspaceHandle, task: dict[str, Any]) -> None:
        # STUB: Commituje zmiany przez GitHub API (Contents API)
        pass

    def integrate_to_target(self, task: dict[str, Any]) -> dict[str, Any]:
        # STUB: Tworzy Pull Request przez GitHub API
        return {"task_id": task["id"], "status": task.get("status"), "pr_url": "https://github.com/.../pull/123"}

    def cleanup_workspace(self, workspace: WorkspaceHandle) -> None:
        # STUB: Usuwa branch na GitHub
        pass