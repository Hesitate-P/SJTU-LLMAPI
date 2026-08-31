#!/bin/sh
# 应用内 VPN：建立仅覆盖交大网段的 IKEv2 隧道并保活。
set -eu

: "${VPN_SERVER:=stu.vpn.sjtu.edu.cn}"
: "${VPN_USERNAME:?需要 jAccount 用户名（VPN_USERNAME）}"
: "${VPN_PASSWORD:?需要 jAccount 密码（VPN_PASSWORD）}"
: "${VPN_ROUTE_SUBNETS:=202.120.0.0/16}"
: "${VPN_EXTRA_HOSTS:=models.sjtu.edu.cn}"
PROBE_URL="${PROBE_URL:-https://models.sjtu.edu.cn/api/v1/models}"

# 1. 额外主机解析为 IP，并入流量选择器
TS="$VPN_ROUTE_SUBNETS"
for host in $VPN_EXTRA_HOSTS; do
    ip="$(dig +short "$host" A | tail -1 || true)"
    if [ -n "$ip" ]; then
        TS="$TS,$ip/32"
        echo "[vpn] $host -> $ip/32 纳入隧道"
    else
        echo "[vpn] 警告: 解析 $host 失败，跳过"
    fi
done
export VPN_TS="$TS"
echo "[vpn] 流量选择器: $TS"

# 2. 渲染 swanctl 配置（密码不落日志）
envsubst '${VPN_SERVER} ${VPN_USERNAME} ${VPN_PASSWORD} ${VPN_TS}' \
    < /etc/swanctl/swanctl.conf.template > /etc/swanctl/swanctl.conf
chmod 600 /etc/swanctl/swanctl.conf

# 3. 不让 charon 安装任何路由（隧道范围完全由 remote_ts 决定）
printf 'install_routes = no\n' > /etc/strongswan.d/charon/no-routes.conf

# 4. 启动 charon 并加载配置
mkdir -p /var/run/charon
# Debian: starter 在 /usr/sbin/ipsec（上游的 /usr/lib/ipsec/strongswan 不存在）
ipsec start --nofork > /var/log/charon.log 2>&1 &

for i in $(seq 1 30); do
    swanctl --load-all >/dev/null 2>&1 && break
    sleep 1
done
swanctl --load-all
echo "[vpn] 发起连接..."
swanctl --initiate --child sjtu || echo "[vpn] 首次发起失败，进入保活重试"

# 5. 保活：周期探测，失败重连
while true; do
    # 注意：curl 失败时 -w 仍会输出 000，若用 `|| echo 000` 会拼成 "000000"，
# 导致判等失败、把不可达误报为可达；这里对整体命令替换做 fallback。
code="$(curl -m 8 -s -o /dev/null -w '%{http_code}' "$PROBE_URL")" || code=000
    if [ "$code" = "000" ]; then
        echo "$(date '+%F %T') [vpn] 探测失败($code)，重连隧道"
        swanctl --terminate --child sjtu >/dev/null 2>&1 || true
        sleep 2
        swanctl --initiate --child sjtu >/dev/null 2>&1 || true
    else
        echo "$(date '+%F %T') [vpn] 探测 OK (HTTP $code)"
    fi
    sleep 30
done
