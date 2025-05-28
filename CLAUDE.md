# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ComfyDeploy API is a FastAPI-based backend service that enables deployment and management of ComfyUI workflows via Modal serverless infrastructure. It handles workflow execution, machine management, file storage, and user subscriptions.

## Architecture

- **Backend**: FastAPI with async PostgreSQL (SQLAlchemy/SQLModel)
- **Database ORM**: Drizzle (TypeScript) for schema and migrations
- **Compute**: Modal serverless platform for GPU workloads
- **Analytics**: ClickHouse with Atlas migrations
- **Caching**: Redis for sessions and caching
- **Storage**: S3-compatible file storage
- **Auth**: JWT with API keys and Stripe subscriptions

## Common Commands

### Development
```bash
bun run dev                 # Start dev server on port 3011
uv run src/api/server.py   # Start API server directly
uv sync                    # Install Python dependencies
```

### Database
```bash
bun run migrate-local      # Run Drizzle migrations (local)
bun run migrate-production # Run Drizzle migrations (production)
bun run generate           # Generate Drizzle schema from schema.ts
```

### Testing
```bash
bun run test              # Run full Docker Compose test suite
uv run pytest -s         # Run Python tests directly
```

### Docker
```bash
docker-compose up         # Local environment (PostgreSQL, Redis, ClickHouse)
```

## Key Architecture Patterns

### Modal Integration
- `/src/modal_apps/` contains ComfyUI Modal applications (Flux, SD3.5, etc.)
- `/src/api/modal/` contains Modal deployment configurations (v3, v4)
- Each Modal app provides GPU-accelerated ComfyUI execution

### API Structure
- **Public API**: Main endpoints at `/` with Scalar documentation
- **Internal API**: Admin endpoints at `/internal`
- **Routes**: Organized by feature (runs, workflows, machines, deployments)
- **Middleware**: Auth, subscription limits, spend tracking

### Database Schema
- **Main DB**: PostgreSQL with comprehensive schema in `schema.ts`
- **Migrations**: Drizzle migrations in `/drizzle/` (currently at 0193)
- **Analytics**: ClickHouse in `/src/clickhouse/` with Atlas migrations

### File Handling
- **Utils**: `/src/api/utils/` contains storage helpers, input/output processing
- **S3 Integration**: Configurable storage backends with presigned URLs
- **Multi-level caching**: Implemented for performance optimization

## Development Environment

- **API Server**: Port 8000
- **Dev Server**: Port 3011 (with auto-reload)
- **PostgreSQL**: Port 5480
- **Redis**: Port 6379
- **ClickHouse**: Port 8123

## Testing

Tests are located in `/tests/api/routes/` and cover API endpoints. Use Docker Compose for integration testing with all services running.