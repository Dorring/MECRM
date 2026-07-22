# syntax=docker/dockerfile:1.7
FROM node:26-bullseye AS builder
WORKDIR /app
RUN apt-get update \
  && apt-get upgrade -y \
  && apt-get install -y --no-install-recommends openssl libssl1.1 \
  && rm -rf /var/lib/apt/lists/*

COPY package*.json ./
COPY prisma ./prisma/
COPY prisma.config.ts ./
RUN --mount=type=cache,target=/root/.npm npm ci

RUN npx prisma generate

COPY . .
RUN npm run build

FROM node:26-bullseye AS runner

WORKDIR /app
ENV NODE_ENV=production

# Keep runtime OS packages current so Trivy's actionable CRITICAL gate only
# fails on vulnerabilities that remain after Debian security updates.
RUN apt-get update \
  && apt-get upgrade -y \
  && apt-get install -y --no-install-recommends openssl libssl1.1 \
  && rm -rf /var/lib/apt/lists/*

COPY package*.json ./
COPY prisma ./prisma/
COPY prisma.config.ts ./
RUN --mount=type=cache,target=/root/.npm npm ci --omit=dev

# The gateway runner only needs `node` to execute dist/index.js.
# Remove npm (and its transitive deps including tar@7.5.16) to eliminate
# CVE-2026-59873 from the runtime image.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

COPY --from=builder /app/dist ./dist

# Ensure the node user can read the app directory
RUN chown -R node:node /app

USER node

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD node -e "fetch('http://localhost:4000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/index.js"]
