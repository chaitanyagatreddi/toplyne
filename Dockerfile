FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg2 \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 \
    libgtk-3-0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY . .

EXPOSE 7860

# Shell form so $PORT (set by Render) expands; falls back to 7860 locally / on HF.
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-7860} --timeout 180 --workers 2
