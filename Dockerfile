FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLOOD_DATA_DIR=/app/state \
    FLOOD_DEMO=0 \
    TZ=Asia/Shanghai

# Chinese map labels and timezone data; no host permission changes.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 flood \
    && useradd --uid 10001 --gid 10001 --no-create-home flood

# Install CPU torch ONCE, before runtime, never the default Linux CUDA wheel.
COPY requirements-cpu.txt requirements-runtime.txt ./
RUN python -m pip install --no-cache-dir -r requirements-cpu.txt \
    && python -m pip install --no-cache-dir -r requirements-runtime.txt \
    && python -m pip check

# .dockerignore excludes credentials, user state, archives and training sources.
# Keep application code root-owned; only state is writable by the runtime user.
COPY . .
RUN mkdir -p /app/state && chown 10001:10001 /app/state
VOLUME ["/app/state"]
USER 10001:10001
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=3).close()"
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
