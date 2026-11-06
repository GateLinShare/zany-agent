#!/bin/sh
# 首启引导 + 双网关守护(同容器主备两进程):
# 1) 代码目录没有 .git 时从挂载仓库 /opt/repo 克隆(clone 到临时目录再拷入: 目录里已挂 .env 非空)
# 2) 各自 while 循环守护: 单进程崩了 3s 后自动拉起(拿磁盘当前代码), 改码重启走 restart 工具/compose restart
# 3) 已有 clone 原样放行 —— 本地改动/改坏的代码都保留, 主备互不覆盖(重生删宿主 code 目录重启)
set -e
clone() {
    if [ ! -e "$1/.git" ]; then
        [ -e /opt/repo/HEAD ] || { echo "错误: $1 无代码且 /opt/repo 没挂到仓库" >&2; exit 1; }
        seed="/tmp/zany-seed.$$"
        git clone -q /opt/repo "$seed"
        mkdir -p "$1"
        cp -a "$seed/." "$1/"
        rm -rf "$seed"
    fi
}
clone /opt/code/zany-primary
clone /opt/code/zany-standby

run() {  # $1=代码目录 $2=该实例 HOME(会话/记忆隔离)
    while true; do
        cd "$1" && HOME="$2" python gateway/main.py || true
        echo "gateway($1) 退出, 3s 后拉起" >&2
        sleep 3
    done
}
run /opt/code/zany-primary /opt/data/zany-primary &
run /opt/code/zany-standby /opt/data/zany-standby &
wait
