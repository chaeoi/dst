FROM debian:bookworm-slim

RUN apt update && apt install -y \
    lib32gcc-s1 \
    libcurl4-gnutls-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root/steam/steamcmd

RUN wget -qO- "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz" | tar -xz

COPY run_dedicated_servers.sh /root/steam/run_dedicated_servers.sh

RUN chmod +x /root/steam/run_dedicated_servers.sh

CMD ["/root/steam/run_dedicated_servers.sh"]
