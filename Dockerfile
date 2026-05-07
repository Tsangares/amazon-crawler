FROM python:3.11-slim

# Chromium + Xvfb + minimal X11 libs for the CCC Cloudflare-bypass setup.
# Mesa/llvmpipe is what lets WebGL report as available without a real GPU.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        xvfb \
        procps \
        fonts-liberation \
        libnss3 \
        libxss1 \
        libasound2 \
        libgbm1 \
        libdrm2 \
        libgl1-mesa-dri \
        mesa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache/pip

COPY main.py ./
COPY scrapers ./scrapers

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    CAMEL_PROFILE_DIR=/tmp/camel-chrome-profile

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
