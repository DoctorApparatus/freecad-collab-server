# syntax=docker/dockerfile:1
FROM rust:1.85-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked && cp target/release/freecad-collab-server /freecad-collab-server

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /data collab \
    && mkdir -p /data && chown collab /data
COPY --from=build /freecad-collab-server /usr/local/bin/freecad-collab-server
USER collab
WORKDIR /data
ENV COLLAB_BIND_ADDR=0.0.0.0:14008 \
    COLLAB_DATABASE_URL=sqlite:///data/collab.db \
    COLLAB_LOG_FORMAT=json
EXPOSE 14008
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s CMD curl -fsS http://127.0.0.1:14008/readyz || exit 1
ENTRYPOINT ["freecad-collab-server"]
CMD ["serve"]
