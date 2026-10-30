FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates lib32gcc-s1 lib32stdc++6 libstdc++6 libcurl3-gnutls lua5.1 python3 wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/steamcmd

RUN wget -qO- https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar -xz

WORKDIR /app
COPY backend /app/backend
COPY frontend /app/frontend
COPY defaults /app/defaults
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data", "/opt/dst/server"]
EXPOSE 8080/tcp 10999/udp 10998/udp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('DST_WEB_PORT','8080')+'/healthz', timeout=3)"
CMD ["python3", "-m", "backend.server"]
