# Base image pinned by digest (node:22-alpine, 2026-10-21): a re-tagged image can't
# slip in. Bump deliberately: docker buildx imagetools inspect node:22-alpine
FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /app
COPY package.json package-lock.json ./
# No dependency install scripts: nothing from npm runs at build time.
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY tsconfig.json ./
COPY src ./src
COPY scripts ./scripts
COPY web ./web
# The build also copies the pinned LiveKit browser SDK into web/vendor/ (browser client).
RUN npm run build && npm prune --omit=dev --ignore-scripts

FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914
ENV NODE_ENV=production AGENTRING_DATA_DIR=/data PORT=8080
# The relay only needs node: drop the package managers from the runtime image.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
      /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg \
 && mkdir -p /data && chown node:node /data
WORKDIR /app
COPY --from=build --chown=root:root /app/node_modules ./node_modules
COPY --from=build --chown=root:root /app/dist ./dist
COPY --from=build --chown=root:root /app/web ./web
COPY --chown=root:root package.json ./
# Code owned by root and not writable by the process; only /data belongs to it.
USER node
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["node", "--disable-warning=ExperimentalWarning", "dist/main.js"]
