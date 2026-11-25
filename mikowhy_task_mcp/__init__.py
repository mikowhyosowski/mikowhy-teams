"""Mikowhy Task MCP - MCP Server + TaskSourcePort Adapters.

Pakiet dostarcza:
- Serwer MCP (FastMCP) udostępniający narzędzia do zarządzania zadaniami
- Adaptery TaskSourcePort: SQLite, Memory, MCP, GitHub, Linear

Instalacja:
    pip install -e .

Użycie jako adapter w mikowhy-teams:
    TASK_PROVIDER=sqlite  # lub memory, mcp, github, linear
    # wymaga: pip install mikowhy-task-mcp

Entry points (auto-discovery przez engine.container.AppContainer):
    task:sqlite = mikowhy_task_mcp.adapters.sqlite:SqliteTaskAdapter
    task:memory = mikowhy_task_mcp.adapters.memory:InMemoryTaskAdapter
    task:mcp    = mikowhy_task_mcp.adapters.mcp:McpTaskAdapter
    task:github = mikowhy_task_mcp.adapters.github:GitHubTaskAdapter
    task:linear = mikowhy_task_mcp.adapters.linear:LinearTaskAdapter
"""