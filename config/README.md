# 账号配置模板

用来生成 GitHub Secret `ANYROUTER_ACCOUNTS` 的值。

## 用法

```bash
cp config/accounts.example.json config/accounts.json
vim config/accounts.json        # 填入自己的账号
./config/build.sh               # 校验 + 压成单行 + 复制到剪贴板
```

然后把输出粘贴到仓库 Settings → Secrets and variables → Actions → `ANYROUTER_ACCOUNTS`。

`build.sh` 会先校验 JSON 语法和字段完整性，格式错了会直接指出问题，避免推上去才发现配置是坏的。

> `config/accounts.json` 和 `config/oneline.txt` 含密码，已在 `.gitignore` 中忽略，不会被提交。

## 两种登录方式

**邮箱密码（推荐）** —— 脚本用浏览器自动登录，不会过期，`api_user` 也会自动获取：

```json
{
  "name": "备注名",
  "provider": "anyrouter",
  "email": "your@email.com",
  "password": "your_password"
}
```

**session cookie** —— 适合用 GitHub / Linux.do 等 OAuth 注册、没设过密码的账号。约 1 个月过期，过期后需要重新取值：

```json
{
  "name": "备注名",
  "provider": "anyrouter",
  "cookies": { "session": "..." },
  "api_user": "12345"
}
```

取值方法：浏览器登录后按 F12，`session` 在 Application → Cookies 里；`api_user` 在 Network 面板任意请求的 `New-Api-User` 请求头里，正常是 5 位数。

## 字段说明

| 字段 | 必需 | 说明 |
|---|---|---|
| `email` + `password` | 二选一 | 浏览器自动登录 |
| `cookies` + `api_user` | 二选一 | session 登录 |
| `provider` | 否 | `anyrouter`（默认）或 `agentrouter` |
| `name` | 否 | 备注名，用于日志和通知，不填则显示 `Account 1` |

两种方式可以混用，一个数组里既能有邮箱密码的账号，也能有 session 的账号。同时填了 `email`+`password` 和 `cookies` 时，优先用邮箱密码登录，session 作为备选。
