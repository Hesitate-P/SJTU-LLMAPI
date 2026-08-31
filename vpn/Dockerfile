FROM debian:bookworm-slim

# Debian bookworm 拆包说明：strongswan-starter 提供 /usr/sbin/ipsec（无 /usr/lib/ipsec/strongswan），
# swanctl CLI 在 strongswan-swanctl（附带 vici 插件），
# eap-mschapv2 在 libcharon-extauth-plugins（libcharon-extra-plugins 不含它，实测确认）。
# 校园网内 deb.debian.org 实测不可达（apt update 卡死/超时），换交大镜像——本项目部署前提本就是
# 校园网/交大 VPN 环境。
# CA pinning（有意为之）：IKE 服务端认证只信任 ISRG Root X1 这一个锚点——spike（2026-08-31）实证
# stu.vpn.sjtu.edu.cn 的证书链为 Let's Encrypt（end-entity ← YR2 ← Root YR ← ISRG Root X1）。
# 不信任全量系统 CA：否则任何公共 CA 给攻击者签发的 stu.vpn.sjtu.edu.cn 证书都能通过校验。
# strongSwan 不读系统信任库，且每个文件只加载一张证书，故从 ca-certificates 包内单文件复制。
# 若学校更换 CA/锚点，需同步更换此文件并重新 spike 验证。
RUN sed -i 's|deb.debian.org|mirror.sjtu.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        strongswan libstrongswan-extra-plugins libcharon-extra-plugins libcharon-extauth-plugins \
        strongswan-swanctl curl iproute2 dnsutils gettext-base ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/swanctl/x509ca \
    && cp /usr/share/ca-certificates/mozilla/ISRG_Root_X1.crt /etc/swanctl/x509ca/

COPY swanctl/ /etc/swanctl/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
