# Frontend: nginx + статический SPA. Конфигурация nginx монтируется под архитектуру
# (infrastructure/generated/nginx/*.conf): 3-tier — путь запроса; C и D — только UI.
#   docker build -f infrastructure/docker/frontend.Dockerfile -t arch-bench-frontend:2.0.0 .
ARG NGINX_IMAGE=nginx:1.27
FROM ${NGINX_IMAGE}
COPY apps/frontend/static /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf
EXPOSE 8080
