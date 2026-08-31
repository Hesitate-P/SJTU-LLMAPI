#!/bin/sh
# 应用内 VPN：与学校网关建立 IKEv2 隧道（服务端强制全隧道 TS），
# 再用路由把"走隧道的流量"限制回交大网段，保活并自愈。
set -eu

: "${VPN_SERVER:=stu.vpn.sjtu.edu.cn}"
: "${VPN_USERNAME:?需要 jAccount 用户名（VPN_USERNAME）}"
: "${VPN_PASSWORD:?需要 jAccount 密码（VPN_PASSWORD）}"
: "${VPN_ROUTE_SUBNETS:=202.120.0.0/16}"
: "${VPN_EXTRA_HOSTS:=models.sjtu.edu.cn}"
PROBE_URL="${PROBE_URL:-https://models.sjtu.edu.cn/api/v1/models}"

# 1. 计算需要走隧道的网段清单（交大网段 + 额外主机解析出的 /32）。
#    注意：这些是【路由】清单，不是协商 TS——学校网关要求 CHILD TS 必须是 0.0.0.0/0
#    （收窄 TS 能协商成功但数据面被静默丢弃，2026-08-31 spike 实测）。
ROUTE_TARGETS="$VPN_ROUTE_SUBNETS"
for host in $VPN_EXTRA_HOSTS; do
    ip="$(dig +short "$host" A | tail -1 || true)"
    if [ -n "$ip" ]; then
        ROUTE_TARGETS="$ROUTE_TARGETS,$ip/32"
        echo "[vpn] $host -> $ip/32 纳入隧道路由"
    else
        echo "[vpn] 警告: 解析 $host 失败，跳过"
    fi
done
echo "[vpn] 隧道路由网段: $ROUTE_TARGETS"

# 2. 渲染 swanctl 配置（密码不落日志）
envsubst '${VPN_SERVER} ${VPN_USERNAME} ${VPN_PASSWORD}' \
    < /etc/swanctl/swanctl.conf.template > /etc/swanctl/swanctl.conf
chmod 600 /etc/swanctl/swanctl.conf

# 3. 内核/插件配置。
# 3a. install_routes = no：TS 是 0.0.0.0/0，若让 charon 装路由会写入 table 220 默认路由，
#     把容器全部出流量（含备用供应商直连）吸进隧道。
#     注意两点坑：① strongswan.d/charon/*.conf 被包含在 charon.plugins{} 内，只能放插件
#     选项；② install_routes 恰恰是 charon 核心选项（kernel_netlink 读的是
#     charon.install_routes），所以必须追加到 /etc/strongswan.conf（重复段落会合并）。
#     "只让交大网段走隧道"由下面 ensure_routes() 维护 table 220 路由实现。
printf '\ncharon {\n    install_routes = no\n}\n' >> /etc/strongswan.conf
# 3b. 禁止 resolve 插件改写 /etc/resolv.conf：学校下发的 DNS 含纯 IPv6 项，容器无 IPv6，
#     改写后 resolv.conf 只剩不可达的 IPv6 服务器，域名解析全挂（探测 000 → 保活循环反复拆隧道）。
#     该插件（5.9.8）没有 resolve=no 开关，仅有 file / resolvconf.iface_prefix 两个选项，
#     故把 file 指到 /dev/null 釜底抽薪；容器继续用 Docker 内置 DNS（127.0.0.11 → 宿主解析器）。
printf 'resolve {\n    file = /dev/null\n}\n' > /etc/strongswan.d/charon/zz-resolve.conf

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

# 5. 隧道路由维护：对每个网段，经容器网关走 table 220，源地址用当前 VIP。
#    内核按"src=VIP 命中 xfrm policy(src=VIP/32,dst=0/0)"决定进隧道；
#    其余流量走 main 表（src=容器 IP，不命中 policy）直连。
#    VIP 由学校网关动态分配且重拨会变，所以每次保活循环都刷新。
ensure_routes() {
    vip="$(ip -4 addr show dev eth0 | awk '/inet /{print $2}' | cut -d/ -f1 | grep -v '^172\.' | tail -1)"
    if [ -z "$vip" ]; then
        return 0  # 隧道未建立（无 VIP），无可操作
    fi
    gw="$(ip route show default | awk '{print $3; exit}')"
    ip rule show | grep -q "lookup 220" || ip rule add priority 220 table 220
    oldIFS="$IFS"; IFS=','
    for net in $ROUTE_TARGETS; do
        ip route replace "$net" via "$gw" dev eth0 src "$vip" table 220 2>/dev/null \
            || echo "[vpn] 警告: 添加路由 $net 失败"
    done
    IFS="$oldIFS"
    ip route flush cache 2>/dev/null || true
}
ensure_routes
# 停机清理：docker stop/restart 先发 SIGTERM。若不主动发 IKE DELETE，学校网关要等
# 会话超时（实测 4~8 分钟）才回收，期间重拨一律 EAP FAIL（单会话限制）。
term_handler() {
    swanctl --terminate --ike sjtu >/dev/null 2>&1 || true
    sleep 1
    exit 0
}
trap term_handler TERM INT

# 6. 保活：周期探测，失败重连。
#    注意：校园网内探测 URL 直连也能通（401），HTTP 码区分不了"走隧道"与"直连"，
#    因此先看 CHILD_SA 是否 INSTALLED：未安装必重连；已安装且探测 000 才重连。
#    另：学校网关疑似单会话限制——容器被强杀（未发 IKE DELETE）后立刻重拨，
#    会出现 MSCHAPv2 成功但 EAP 最终 FAIL，需重试等待网关回收旧会话（实测数分钟内恢复）。
while true; do
    # 注意：curl 失败时 -w 仍会输出 000，若用 `|| echo 000` 会拼成 "000000"，
    # 导致判等失败、把不可达误报为可达；这里对整体命令替换做 fallback。
code="$(curl -m 8 -s -o /dev/null -w '%{http_code}' "$PROBE_URL")" || code=000
    if ! swanctl --list-sas 2>/dev/null | grep -q "INSTALLED"; then
        echo "$(date '+%F %T') [vpn] CHILD_SA 未安装（探测码 $code 仅代表直连可达性），重连隧道"
        swanctl --terminate --child sjtu >/dev/null 2>&1 || true
        sleep 2
        swanctl --initiate --child sjtu >/dev/null 2>&1 || true
    elif [ "$code" = "000" ]; then
        echo "$(date '+%F %T') [vpn] 探测失败($code)，重连隧道"
        swanctl --terminate --child sjtu >/dev/null 2>&1 || true
        sleep 2
        swanctl --initiate --child sjtu >/dev/null 2>&1 || true
    else
        echo "$(date '+%F %T') [vpn] 探测 OK (HTTP $code)"
    fi
    ensure_routes
    # sleep 放后台 + wait：wait 可被信号打断，SIGTERM 到达时 trap 立即执行；
    # 若直接前台 sleep 30，要等 sleep 结束才跑 trap，早被 docker 的 10s 宽限 SIGKILL 了。
    sleep 30 &
    wait $!
done
