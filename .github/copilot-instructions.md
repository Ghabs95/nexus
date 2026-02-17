# Copilot Instructions - Nexus

## Team Memory

- Coding conventions: PEP 8, 100-char lines, double quotes, type hints on function signatures; prefer small, readable functions; avoid clever abstractions; minimal purposeful comments.
- Docs refinement: Update README and docs whenever behavior, configuration, or deployment steps change.
- Architecture guardrails: Keep Telegram bot, inbox processor, and health check responsibilities separated; do not break workflow state/data contracts; avoid cross-module coupling without explicit review.
- Release workflow: Feature branches + PRs; tests must pass; update changelog/notes for user-visible changes; commit messages follow type/scope format.
- Ownership/routing: Infra/service changes go to ops/infrastructure owners; core bot/processor logic goes to platform/app owners; flag complex workflow changes for senior review.
- Security check: Never commit secrets (tokens, API keys, passwords); keep vars.secret untracked; scrub logs before sharing.
