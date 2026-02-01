FROM node:lts-alpine AS builder

WORKDIR /metube
COPY ui ./
RUN corepack enable && corepack prepare pnpm --activate
RUN pnpm install && pnpm run build


FROM python:3.13-alpine

WORKDIR /app

COPY pyproject.toml uv.lock docker-entrypoint.sh ./

# Use sed to strip carriage-return characters from the entrypoint script (in case building on Windows)
# Install dependencies
RUN sed -i 's/\r$//g' docker-entrypoint.sh && \
    chmod +x docker-entrypoint.sh && \
    apk add --update ffmpeg aria2 coreutils shadow su-exec curl tini deno gdbm-tools sqlite file libstdc++ && \
    apk add --update --virtual .build-deps gcc g++ musl-dev uv && \
    UV_PROJECT_ENVIRONMENT=/usr/local uv sync --frozen --no-dev --compile-bytecode && \
    apk del .build-deps && \
    rm -rf /var/cache/apk/* && \
    mkdir /.cache && chmod 777 /.cache

# Install N_m3u8DL-RE for HLS downloads (handles headers properly)
ARG TARGETARCH
RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "x64") && \
    curl -L "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-musl-${ARCH}_20251029.tar.gz" \
    -o /tmp/n_m3u8dl.tar.gz && \
    tar -xzf /tmp/n_m3u8dl.tar.gz -C /usr/local/bin && \
    chmod +x /usr/local/bin/N_m3u8DL-RE && \
    rm /tmp/n_m3u8dl.tar.gz

# Install nightly yt-dlp (override stable version from uv sync)
RUN pip install --break-system-packages --no-deps --pre -U yt-dlp

COPY app ./app
COPY --from=builder /metube/dist/metube ./ui/dist/metube

ENV UID=1000
ENV GID=1000
ENV UMASK=022

ENV DOWNLOAD_DIR /downloads
ENV STATE_DIR /downloads/.metube
ENV TEMP_DIR /downloads
VOLUME /downloads
EXPOSE 8081

# Add build-time argument for version
ARG VERSION=dev
ENV METUBE_VERSION=$VERSION

ENTRYPOINT ["/sbin/tini", "-g", "--", "./docker-entrypoint.sh"]
