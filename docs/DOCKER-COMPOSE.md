# Nexus Docker Compose

This is the deployment-focused Compose stack for the `nexus` repo.

## Purpose

- `nexus/docker-compose.yml` is the runtime stack for your deployed bot.
- `nexus-core/examples/telegram-bot/docker-compose.yml` remains a generic template.

## Prerequisites

- Docker + Compose plugin installed
- `.env` present in this folder
- `BASE_DIR` in `.env` points to your host workspace root

## Run

```bash
cd /home/ubuntu/git/ghabs/nexus
cp .env.example .env  # first time only
# edit .env with real secrets

docker compose up -d --build
```

Or use the deploy wrapper (reads `DEPLOY_TYPE` from `.env`):

```bash
./scripts/deploy.sh up
```

## Operations

```bash
docker compose ps
docker compose logs -f bot processor webhook health
docker compose restart processor
docker compose down
```

## Config-driven deploy mode

Set in `.env`:

- `DEPLOY_TYPE=compose` → uses Docker Compose
- `DEPLOY_TYPE=systemd` → uses `nexus-*.service` units

Wrapper usage:

```bash
./scripts/deploy.sh [up|down|restart|status|logs]
```

## Notes

- Compose loads env from `./.env`.
- Runtime code is built from `../nexus-core/examples/telegram-bot`.
- Runtime config is forced to `/app/config/project_config.yaml` (mounted from `./config`).
- Host workspaces are mounted from `${BASE_DIR}` so agent workflows can access repositories.
