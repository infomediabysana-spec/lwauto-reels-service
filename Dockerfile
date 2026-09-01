FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render provides $PORT at runtime; default to 10000 for local testing.
ENV PORT=10000
EXPOSE 10000

# Long timeout: a reel render (download photos + TTS + ffmpeg encode) can take
# 30-90s. -w 1 keeps memory low on the free instance; Render only sends one
# request at a time from Make.com anyway.
CMD gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 150 server:app
