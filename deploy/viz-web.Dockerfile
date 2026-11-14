# syntax=docker/dockerfile:1.7
# Hold Short visualizer web app: Vite build, served by nginx with SPA fallback.
#   docker build -f deploy/viz-web.Dockerfile \
#     --build-arg VITE_VIZ_API=http://localhost:8100 \
#     --build-arg VITE_TOWER_API=http://localhost:8000 -t holdshort-viz-web .
# The API URLs are baked in at build time (Vite inlines import.meta.env).
FROM node:20-slim AS build
ARG VITE_VIZ_API=http://localhost:8100
ARG VITE_TOWER_API=http://localhost:8000
ENV VITE_VIZ_API=${VITE_VIZ_API} \
    VITE_TOWER_API=${VITE_TOWER_API} \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /web
COPY visualizer/web/package.json visualizer/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY visualizer/web/ ./
RUN npm run build

FROM nginx:1.27-alpine
ENV PORT=8080
# nginx's image runs envsubst on /etc/nginx/templates/*.template at start-up.
RUN mkdir -p /etc/nginx/templates && rm -f /etc/nginx/conf.d/default.conf \
 && printf '%s\n' \
  'server {' \
  '    listen ${PORT};' \
  '    root /usr/share/nginx/html;' \
  '    index index.html;' \
  '    location /assets/ { add_header Cache-Control "public, max-age=31536000, immutable"; try_files $uri =404; }' \
  '    location = /index.html { add_header Cache-Control "no-cache"; }' \
  '    location / { try_files $uri $uri/ /index.html; }' \
  '}' > /etc/nginx/templates/default.conf.template
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- "http://127.0.0.1:${PORT:-8080}/" >/dev/null || exit 1
