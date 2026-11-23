"""FLOW: config-based przepływ pracy zespołu (zakładka 🌊 Flow w dashboardzie).

load_config()  — default_flow.json + deep-merge override .agents/flow/flow.config.json
build_flow()   — model diagramu dla /api/flow (etapy + zadania + przejścia + akcje)
apply_action() — wykonanie akcji Sponsora zdefiniowanej w configu (approve/decline/retry/…)
"""
