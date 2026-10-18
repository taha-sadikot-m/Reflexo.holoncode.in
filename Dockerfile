# The web app (public site, owner app, ops console): built once, served by the API.
FROM node:20.18-slim AS web
WORKDIR /web
COPY apps/admin/package.json apps/admin/package-lock.json apps/admin/.npmrc ./
# npm can crash yet exit 0 ("Exit handler never called"): check the toolchain is really there
RUN npm ci --no-audit --no-fund && test -x node_modules/.bin/tsc && test -x node_modules/.bin/vite
COPY apps/admin/ ./
RUN npm run build

# The rule checker (BETA_04 §7.6): the same stages as tools/policy-check/Dockerfile.
FROM rust:1.90.0-bookworm AS policy-check
WORKDIR /src
COPY tools/policy-check/Cargo.toml tools/policy-check/Cargo.lock ./
COPY tools/policy-check/src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim AS solver
ARG CVC5_VERSION=1.3.1
ARG CVC5_SHA256=1a1cda20d2df4938fa4944a69f33ddc9172e319ece0eed0aa09c4d7abede3ed1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/cvc5.zip       "https://github.com/cvc5/cvc5/releases/download/cvc5-${CVC5_VERSION}/cvc5-Linux-x86_64-static.zip"  && echo "${CVC5_SHA256}  /tmp/cvc5.zip" | sha256sum -c -  && unzip -q /tmp/cvc5.zip -d /tmp/cvc5  && install -m 0755 "$(find /tmp/cvc5 -type f -name cvc5 -path '*/bin/*' | head -1)" /usr/local/bin/cvc5

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/apps/admin/dist
COPY --from=solver /usr/local/bin/cvc5 /usr/local/bin/cvc5
COPY --from=policy-check /src/target/release/reflexo-policy-check /usr/local/bin/reflexo-policy-check
ENV CVC5=/usr/local/bin/cvc5
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["uvicorn", "reflexo.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
