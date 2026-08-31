FROM debian:bookworm-slim

# Debian bookworm 拆包说明：strongswan-starter 提供 /usr/sbin/ipsec（无 /usr/lib/ipsec/strongswan），
# swanctl CLI 在 strongswan-swanctl（附带 vici 插件），
# eap-mschapv2 在 libcharon-extauth-plugins（libcharon-extra-plugins 不含它，实测确认）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        strongswan libstrongswan-extra-plugins libcharon-extra-plugins libcharon-extauth-plugins \
        strongswan-swanctl curl iproute2 dnsutils gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY swanctl/ /etc/swanctl/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
