# The web app (public site, owner app, ops console): built once, served by the API.
FROM node:20.18-slim AS web
WORKDIR /web
COPY apps/admin/package.json apps/admin/package-lock.json apps/admin/.npmrc ./
# npm can crash yet exit 0 ("Exit handler never called"): check the toolchain is really there
RUN npm ci --no-audit --no-fund && test -x node_modules/.bin/tsc && test -x node_modules/.bin/vite
COPY apps/admin/ ./
RUN npm run build

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/apps/admin/dist
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["uvicorn", "reflexo.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
