#!/usr/bin/env bash
# 通过 mihomo 拉取订阅、启动本地代理并探测可用节点。
# 环境变量:
#   PROXY_SUBSCRIPTION_URLS 多个订阅链接，每行一个（优先）
#   PROXY_SUBSCRIPTION_URL  单个订阅链接（兼容旧配置）
#   PROXY_SUBSCRIPTION_FILE 本地订阅清单，默认 .proxy-subscriptions
#   PROXY_TEST_URL          探测目标，默认 https://www.google.com/generate_204
#   PROXY_REQUIRED          true 时探测失败则退出 1
#   PROXY_PORT              本地 mixed-port，默认 7890

set -euo pipefail

SUBSCRIPTION_FILE="${PROXY_SUBSCRIPTION_FILE:-.proxy-subscriptions}"
if [[ -n "${PROXY_SUBSCRIPTION_URLS:-}" ]]; then
	SUBSCRIPTION_INPUT="${PROXY_SUBSCRIPTION_URLS}"
elif [[ -n "${PROXY_SUBSCRIPTION_URL:-}" ]]; then
	SUBSCRIPTION_INPUT="${PROXY_SUBSCRIPTION_URL}"
elif [[ -f "${SUBSCRIPTION_FILE}" ]]; then
	SUBSCRIPTION_INPUT="$(< "${SUBSCRIPTION_FILE}")"
else
	SUBSCRIPTION_INPUT=""
fi
SUBSCRIPTION_URLS=()
while IFS= read -r subscription_url; do
	subscription_url="${subscription_url%$'\r'}"
	if [[ -n "${subscription_url//[[:space:]]/}" ]]; then
		SUBSCRIPTION_URLS+=("${subscription_url}")
	fi
done <<< "${SUBSCRIPTION_INPUT}"

if (( ${#SUBSCRIPTION_URLS[@]} == 0 )); then
	echo "[INFO] Proxy subscription not set, skip proxy setup"
	exit 0
fi

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.27}"
PROXY_REQUIRED="${PROXY_REQUIRED:-false}"

mkdir -p "${PROXY_DIR}"
cd "${PROXY_DIR}"

echo "[INFO] Downloading mihomo ${MIHOMO_VERSION}..."
ARCHIVE="mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
if ! curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${ARCHIVE}" \
	"https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/${ARCHIVE}"; then
	echo "[WARN] Failed to download mihomo ${MIHOMO_VERSION}, skip proxy setup"
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi
gunzip -f "${ARCHIVE}"
chmod +x "mihomo-linux-amd64-${MIHOMO_VERSION}"
MIHOMO_BIN="${PROXY_DIR}/mihomo-linux-amd64-${MIHOMO_VERSION}"

PROXY_CONTROLLER_PORT="${PROXY_CONTROLLER_PORT:-9091}"
PROVIDER_NAMES=()

cat > config.yaml <<EOF
mixed-port: ${PROXY_PORT}
external-controller: 127.0.0.1:${PROXY_CONTROLLER_PORT}
allow-lan: false
ipv6: false
mode: rule
log-level: warning
unified-delay: true

proxy-providers:
EOF

for index in "${!SUBSCRIPTION_URLS[@]}"; do
	provider_name="subscription_$((index + 1))"
	PROVIDER_NAMES+=("${provider_name}")
	cat >> config.yaml <<EOF
  ${provider_name}:
    type: http
    url: "${SUBSCRIPTION_URLS[index]}"
    interval: 3600
    path: ./subscription_$((index + 1)).yaml
    health-check:
      enable: true
      interval: 300
      url: https://www.gstatic.com/generate_204
    override:
      additional-prefix: "[sub$((index + 1))] "
EOF
done

cat >> config.yaml <<EOF
proxy-groups:
  # CHECKIN 是 select 组，脚本可通过 Clash API 手动切换出口节点（规避同 IP 限流）
  - name: CHECKIN
    type: select
    proxies:
      - AUTO
    use:
EOF
for provider_name in "${PROVIDER_NAMES[@]}"; do
	printf '      - %s\n' "${provider_name}" >> config.yaml
done
cat >> config.yaml <<EOF
  - name: AUTO
    type: url-test
    url: "${PROXY_TEST_URL}"
    interval: 300
    tolerance: 150
    lazy: false
    use:
EOF
for provider_name in "${PROVIDER_NAMES[@]}"; do
	printf '      - %s\n' "${provider_name}" >> config.yaml
done
cat >> config.yaml <<EOF

rules:
  - MATCH,CHECKIN
EOF
chmod 600 config.yaml
echo "[INFO] Configured ${#SUBSCRIPTION_URLS[@]} proxy subscription(s)"

echo "[INFO] Starting mihomo on 127.0.0.1:${PROXY_PORT}..."
nohup "${MIHOMO_BIN}" -d "${PROXY_DIR}" -f config.yaml > mihomo.log 2>&1 &
echo $! > mihomo.pid

PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
READY=false
for attempt in $(seq 1 45); do
	if curl -fsS -x "${PROXY_URL}" --max-time 20 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	echo "[INFO] Waiting for proxy health check (${attempt}/45)..."
	sleep 2
done

if [[ "${READY}" != "true" ]]; then
	echo "[FAILED] Proxy health check failed for ${PROXY_TEST_URL}"
	# 订阅拉取错误可能带完整 URL，输出前把查询参数（尤其 token）统一脱敏。
	tail -n 30 mihomo.log | sed -E 's#(https?://[^?[:space:]]+)\?[^[:space:]]+#\1?[REDACTED]#g' || true
	if [[ -f mihomo.pid ]]; then
		kill "$(cat mihomo.pid)" 2>/dev/null || true
	fi
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

echo "[SUCCESS] Proxy is ready: ${PROXY_URL}"
if command -v jq >/dev/null 2>&1; then
	provider_status="$(curl -fsS "http://127.0.0.1:${PROXY_CONTROLLER_PORT}/providers/proxies" 2>/dev/null || true)"
	for provider_name in "${PROVIDER_NAMES[@]}"; do
		node_count="$(jq -r --arg name "${provider_name}" '.providers[$name].proxies | length // 0' <<< "${provider_status}" 2>/dev/null || printf '0')"
		if (( node_count > 0 )); then
			echo "[INFO] Proxy provider ${provider_name}: ${node_count} node(s) loaded"
		else
			echo "[WARN] Proxy provider ${provider_name}: no nodes loaded"
		fi
	done
fi
echo "[INFO] Proxy is scoped to CHECKIN_PROXY_URL (browser/python only, not global HTTP_PROXY)"
if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
	echo "CHECKIN_PROXY_CONTROLLER=http://127.0.0.1:${PROXY_CONTROLLER_PORT}" >> "${GITHUB_ENV}"
fi
