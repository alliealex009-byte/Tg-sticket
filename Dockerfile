FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    LOG_DIR=/app/logs

# libgomp1: onnxruntime/rembg · libglib2.0-0: OpenCV (rembg dep) · fonts: text stickers
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

ARG INSTALL_LOCAL_AI=0
COPY requirements-local.txt .
RUN if [ "$INSTALL_LOCAL_AI" = "1" ]; then pip install -r requirements-local.txt --extra-index-url https://download.pytorch.org/whl/cpu; fi

RUN useradd --create-home --uid 10001 stickerbot
COPY --chown=stickerbot:stickerbot . .
RUN mkdir -p /app/data /app/logs && chown -R stickerbot:stickerbot /app/data /app/logs
USER stickerbot

# Bot writes data/heartbeat every 30 s
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import time,sys; sys.exit(0 if time.time()-int(open('/app/data/heartbeat').read())<120 else 1)"

CMD ["python", "-m", "bot.main"]
