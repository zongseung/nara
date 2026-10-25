FROM nginx:stable-alpine@sha256:47928bf67eb62a1863cb08d9ecb52d3e704304ba614eaee17884204677702951
ARG NGINX_CONFIG=docker/nginx.conf
COPY ${NGINX_CONFIG} /etc/nginx/nginx.conf
