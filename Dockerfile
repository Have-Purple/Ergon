# No dependency install: server.py is standard library only, so the image has nothing to fetch and
# nothing to update. The container runs as a non-root user with no shell entrypoint.
FROM python:3.13-slim

RUN useradd --create-home --uid 10001 ergon
WORKDIR /app
COPY server.py /app/server.py
RUN mkdir -p /state /workspace && chown -R ergon:ergon /app /state /workspace

ENV ERGON_STATE=/state \
    ERGON_WORKSPACE=/workspace \
    ERGON_PORT=8000

USER ergon
EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]

CMD ["python", "/app/server.py"]
