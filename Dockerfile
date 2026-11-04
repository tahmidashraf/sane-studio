# SANE Studio: production image. Runs on any container host; deploy/ runs it on
# one Oracle Cloud server behind Caddy.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MNE_DONTWRITE_HOME=true \
    SANE_DATA_DIR=/tmp/sane-studio \
    SANE_TTL_MINUTES=60 \
    SANE_MAX_UPLOAD_MB=1024 \
    SANE_WORKERS=2 \
    PORT=7860

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 sane
WORKDIR /home/sane/app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=sane:sane app ./app
COPY --chown=sane:sane web ./web
COPY --chown=sane:sane samples ./samples

USER sane
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD \
    python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/api/health')"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75"]
