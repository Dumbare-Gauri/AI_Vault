# Build context is the repo root — the frontend depends on workspace
# packages (@vault/types, @vault/config) that live outside apps/frontend.
FROM node:22-slim AS base

WORKDIR /workspace

RUN corepack enable

COPY package.json pnpm-workspace.yaml pnpm-lock.yaml turbo.json ./
COPY packages/config ./packages/config
COPY packages/types ./packages/types
COPY packages/ui ./packages/ui
COPY apps/frontend ./apps/frontend

RUN pnpm install --frozen-lockfile

# ---- development (used by docker-compose.yml for local dev / hot reload) ----
FROM base AS development

WORKDIR /workspace/apps/frontend

EXPOSE 5173

CMD ["pnpm", "dev", "--", "--host", "0.0.0.0"]

# ---- production build ----
FROM base AS build

# Empty API base = same origin: the app calls `/v1/...` on whatever address
# it is served from, so moving from an IP to a domain needs no rebuild.
ARG VITE_API_BASE_URL=""
# The environment's own Google OAuth client (staging and production differ).
ARG VITE_GOOGLE_CLIENT_ID=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID

RUN pnpm --filter @vault/frontend build

# ---- production (static file server) ----
FROM nginx:1.30-alpine AS production

RUN apk upgrade --no-cache

COPY --from=build /workspace/apps/frontend/dist /usr/share/nginx/html
# Rendered at start-up from the container's environment (nginx's
# /etc/nginx/templates mechanism): routes /v1 to the API on the same origin.
COPY infrastructure/single-server/nginx/default.conf.template /etc/nginx/templates/default.conf.template

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- --header "X-Origin-Verify: ${ORIGIN_VERIFY_SECRET}" http://127.0.0.1:80/ || exit 1
