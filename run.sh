#!/usr/bin/env bash
# 一条命令启动：自动预置演示数据，启动本地服务。
# 用法：./run.sh [--keep-data] [--port 8765]
set -euo pipefail
cd "$(dirname "$0")"

PORT=8765
RESEED="--reseed"
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-data) RESEED=""; shift ;;
    --port)      PORT="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) PORT="$1"; shift ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3（仅用标准库，无需安装任何依赖）" >&2
  exit 1
fi

echo "============================================================"
echo " 青铜博物馆 · 语音导览器借还与充电周转系统"
echo " 演示数据自动预置中…  访问地址：http://127.0.0.1:${PORT}"
echo " 顶栏切换 讲解员/消毒充电员/维修员/主管 与馆区，验证权限"
echo "============================================================"
exec python3 -m app $RESEED --port "${PORT}"
