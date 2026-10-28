FROM python:3.13.12-slim

ARG GIT_SHA=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STX_GIT_SHA=${GIT_SHA} \
    STX_HOST_ROOT=/host \
    STX_DATA_DIR=/data \
    STX_PORT=8090

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor ./monitor
COPY web ./web
# The bundled plan. Mount a folder at /config with your own apps.yaml to override it.
COPY config ./config

VOLUME /data
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('STX_PORT', '8090'), timeout=4)"

CMD ["python", "-m", "monitor"]
