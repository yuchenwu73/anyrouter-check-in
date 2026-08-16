#!/usr/bin/env bash
# 把 config/accounts.json 压成单行 JSON，用于粘贴到 GitHub Secret: CHECKIN_ACCOUNTS
#
# 用法：
#   cp config/accounts.example.json config/accounts.json
#   vim config/accounts.json        # 填入自己的账号
#   ./config/build.sh
#
# accounts.json 已被 .gitignore 忽略，不会提交。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/accounts.json"
OUT="$DIR/oneline.txt"

if [ ! -f "$SRC" ]; then
    echo "❌ 找不到 $SRC"
    echo
    echo "请先复制模板再填写："
    echo "  cp $DIR/accounts.example.json $SRC"
    exit 1
fi

ONELINE=$(python3 - "$SRC" <<'PY'
import json, sys

path = sys.argv[1]
try:
    data = json.load(open(path, encoding='utf-8'))
except json.JSONDecodeError as e:
    sys.exit(f'JSON 格式错误: {e}（多半是漏了逗号或引号）')

if not isinstance(data, list) or not data:
    sys.exit('最外层必须是非空数组 [ ]')

for i, acc in enumerate(data, 1):
    name = acc.get('name', f'第{i}个')
    has_login = acc.get('email') and acc.get('password')
    has_cookie = acc.get('cookies') and acc.get('api_user')
    if not has_login and not has_cookie:
        sys.exit(f'账号 {name}: 需要 email+password，或 cookies+api_user，两者至少有一组')

print(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
PY
)

COUNT=$(python3 -c "import json;print(len(json.load(open('$SRC',encoding='utf-8'))))")

printf '%s' "$ONELINE" > "$OUT"
chmod 600 "$OUT"

echo "✅ 校验通过，共 $COUNT 个账号"
echo "📄 已写入：$OUT"

# 尽量复制到剪贴板（WSL / macOS / Linux）
if command -v clip.exe >/dev/null 2>&1; then
    printf '%s' "$ONELINE" | clip.exe && echo "📋 已复制到剪贴板"
elif command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$ONELINE" | pbcopy && echo "📋 已复制到剪贴板"
elif command -v xclip >/dev/null 2>&1; then
    printf '%s' "$ONELINE" | xclip -selection clipboard && echo "📋 已复制到剪贴板"
fi

# 从 git remote 推导出 secret 编辑页地址
SLUG=$(git -C "$DIR" remote get-url origin 2>/dev/null |
    sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##' || true)

echo
if [ -n "${SLUG:-}" ]; then
    echo "粘贴到这里（直达编辑页）："
    echo "  https://github.com/$SLUG/settings/secrets/actions/CHECKIN_ACCOUNTS"
else
    echo "粘贴到：仓库 Settings → Secrets and variables → Actions → CHECKIN_ACCOUNTS"
fi
echo
echo "步骤：清空 Value 输入框 → 粘贴 → Update secret"
