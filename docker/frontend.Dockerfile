# Frontend: Vite build → nginx.
FROM node:25-slim AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Relativní základ (#542) — prohlížeč jde na API přes nginx proxy, ne na port 8000
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE
# Prostředí (#568): compose.dev.yml posílá 'dev' → DEV badge; prod neposílá nic
ARG VITE_GEXLENS_ENV=
ENV VITE_GEXLENS_ENV=$VITE_GEXLENS_ENV
# Verze buildu v exportu měřiče výkonu (#1409); CI posílá commit, lokální build 'dev'
ARG VITE_BUILD_SHA=
ENV VITE_BUILD_SHA=$VITE_BUILD_SHA
RUN npm run build

FROM nginx:1.31-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
