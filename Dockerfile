FROM flyio/flyctl:latest AS flyio

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The Docker backend talks to a mounted host socket. The Fly backend uses the
# official flyctl binary copied from Fly.io's image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY --from=flyio /flyctl /usr/local/bin/fly
RUN ln -s /usr/local/bin/fly /usr/local/bin/flyctl

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN python -m pip install .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["kilntainers"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8080", "--backend", "docker"]
