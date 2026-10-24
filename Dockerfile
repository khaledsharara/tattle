# One image for both containers: the dashboard (`python -m tattle`, the default)
# and, in gateway mode, the gateway (`python -m tattle.gateway run`, as root
# with network capabilities). Built for linux/amd64 and linux/arm64.

# --- dashboard build ----------------------------------------------------------
# The built files are the same on every architecture, so this stage always runs
# natively instead of under emulation.
FROM --platform=$BUILDPLATFORM node:22.11.0-alpine3.20 AS web
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- runtime ------------------------------------------------------------------
# Debian trixie: its iptables (1.8.11) is at least as new as the hosts' own, so
# rules it writes into the host's firewall read back cleanly there.
FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Gateway mode: dnsmasq (DHCP + DNS), nft (counters, NAT), iptables (nft and
# legacy flavours, for Docker's FORWARD chain), ip (detecting the network).
# openssh-client: router adapters that read counters over SSH.
RUN apt-get update \
    && apt-get install -y --no-install-recommends dnsmasq-base nftables iptables iproute2 openssh-client \
    && rm -rf /var/lib/apt/lists/*
# The dashboard runs as uid 1000, which needs a name: ssh refuses to start for a
# uid with no passwd entry ("No user exists for uid 1000").
RUN groupadd --gid 1000 tattle \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /data --shell /usr/sbin/nologin tattle \
    && install -d -o 1000 -g 1000 -m 0755 /data
# /data holds the database and the first-run setup code. Compose mounts ./data
# over it; this one makes a plain `docker run` (e.g. the demo) work too.
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Manufacturer names for MAC prefixes. Optional: the dashboard works without it.
RUN mkdir -p /app/data \
    && (python -c "import shutil, urllib.request as u; shutil.copyfileobj(u.urlopen('https://www.wireshark.org/download/automated/data/manuf', timeout=60), open('/app/data/manuf', 'wb'))" \
        || (rm -f /app/data/manuf; echo "manuf download failed; vendors will be blank"))
COPY backend/tattle ./tattle
COPY --from=web /web/dist ./static
USER 1000:1000
EXPOSE 8090
CMD ["python", "-m", "tattle"]
