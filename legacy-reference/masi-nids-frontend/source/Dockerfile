FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS builder
WORKDIR /app
ARG NIDS_BACKEND_INTERNAL_URL=http://nids-backend:8090
ARG NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID=demo-train
ARG NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID=online-demo
ENV NIDS_BACKEND_INTERNAL_URL=${NIDS_BACKEND_INTERNAL_URL}
ENV NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID=${NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID}
ENV NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID=${NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS runner
WORKDIR /app
ARG NIDS_IMAGE_REVISION=dev
ARG NIDS_IMAGE_SOURCE_TREE=dev
LABEL org.opencontainers.image.revision=${NIDS_IMAGE_REVISION} \
      org.opencontainers.image.source-tree=${NIDS_IMAGE_SOURCE_TREE}
ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=3000
ARG NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID=demo-train
ARG NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID=online-demo
ENV NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID=${NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID}
ENV NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID=${NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID}
COPY package.json package-lock.json ./
COPY --from=deps /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/next.config.ts ./next.config.ts
COPY --from=builder /app/public ./public
RUN npm prune --omit=dev
RUN chown -R node:node /app

USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["npm", "run", "start", "--", "-H", "0.0.0.0", "-p", "3000"]
