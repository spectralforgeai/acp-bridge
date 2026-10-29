FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS build

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/acp_bridge/ ./src/acp_bridge/
RUN uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime
LABEL org.opencontainers.image.source="https://github.com/spectralforgeai/acp-bridge" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 acp-bridge \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin acp-bridge \
    && install -d -o root -g root -m 0755 /run/agent
COPY --from=build /app/.venv /app/.venv
COPY LICENSE /usr/share/doc/acp-bridge/LICENSE
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER acp-bridge:acp-bridge
EXPOSE 8080
ENTRYPOINT ["acp-bridge"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
