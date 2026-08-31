FROM debian:bookworm-slim

# Debian bookworm 拆包说明：strongswan-starter 提供 /usr/sbin/ipsec（无 /usr/lib/ipsec/strongswan），
# swanctl CLI 在 strongswan-swanctl（附带 vici 插件），
# eap-mschapv2 在 libcharon-extauth-plugins（libcharon-extra-plugins 不含它，实测确认）。
# 校园网内 deb.debian.org 实测不可达（apt update 卡死/超时），换交大镜像——本项目部署前提本就是
# 校园网/交大 VPN 环境。ca-certificates：服务器证书为公共 CA（Let's Encrypt）签发；strongSwan
# 不读系统信任库，需把 bundle 复制进 /etc/swanctl/x509ca/ 才能作为 pubkey 认证信任锚（不做 CA pinning）。
# strongSwan 每文件只取一张证书，系统 CA bundle 必须拆成单证书文件才能全部加载。
# 注意：续行符后不能插注释行（会被当作命令结尾，后续 && 全部丢失且不报错）。
RUN sed -i 's|deb.debian.org|mirror.sjtu.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        strongswan libstrongswan-extra-plugins libcharon-extra-plugins libcharon-extauth-plugins \
        strongswan-swanctl curl iproute2 dnsutils gettext-base ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/swanctl/x509ca \
    && cd /etc/swanctl/x509ca \
    && awk 'BEGIN{n=0} /BEGIN CERTIFICATE/{n++;f=sprintf("ca-%03d.crt",n)} {print > f}' \
        /etc/ssl/certs/ca-certificates.crt

COPY swanctl/ /etc/swanctl/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
