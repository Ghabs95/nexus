# Nexus Docker Compose

This is the deployment-focused Compose stack for the `nexus` repo.

## Purpose

- `nexus/docker-compose.yml` is the shared base stack (same service names in all envs).
- `nexus/docker-compose.local.yml` adds local build/image overrides.
- `nexus/docker-compose.prod.yml` adds production image/platform overrides.
- `nexus-arc/examples/nexus-bot/docker-compose.yml` remains a generic template.

## Prerequisites

- Docker + Compose plugin installed
- `.env` present in this folder
- `BASE_DIR` in `.env` points to your host workspace root

## Run

```bash
cd /home/ubuntu/git/ghabs/nexus
cp .env.example .env  # first time only
# edit .env with real secrets

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Or use the deploy wrapper (reads `DEPLOY_TYPE` from `.env`):

```bash
./scripts/deploy.sh up
```

## Operations

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f telegram discord processor webhook health
docker compose -f docker-compose.yml -f docker-compose.local.yml restart processor
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

## Config-driven deploy mode

Set in `.env`:

- `DEPLOY_TYPE=compose` → uses Docker Compose
- `DEPLOY_TYPE=systemd` → uses `nexus-*.service` units

Wrapper usage:

```bash
./scripts/deploy.sh [up|down|restart|status|logs]
./scripts/deploy.sh status --quiet
./scripts/health-check.sh
./scripts/smoke-deploy.sh
```

## Notes

- Compose loads env from `./.env`.
- `COMPOSE_REMOVE_ORPHANS=false` by default in deploy scripts to prevent accidental removal of observability containers.
- Runtime code is built from `../nexus-arc/examples/nexus-bot`.
- Runtime config is forced to `/app/config/project_config.yaml` (mounted from `./config`).
- Host workspaces are mounted from `${BASE_DIR}` so agent workflows can access repositories.
