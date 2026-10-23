FROM python:3.12-slim
# ffprobe checks each title's stream once.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /data
# A new config listens where the published port reaches it.
ENV REX_STREMIO_LISTEN=0.0.0.0:9982
COPY rex_stremio.py /app/rex_stremio.py
EXPOSE 9982
CMD ["python3", "/app/rex_stremio.py", "--config", "/data/config.json"]
