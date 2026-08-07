# Frontend: Vite build → nginx.
FROM node:22-slim AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Relativní základ (#542) — prohlížeč jde na API přes nginx proxy, ne na port 8000
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
