# 用外部定时服务触发签到（解决 Actions 定时不准）

## 这是在解决什么问题

GitHub Actions 的 `schedule`（定时触发）**不保证准时，也不保证一定会跑**。这不是配置问题，是官方明说的设计：

> Scheduled workflows may be delayed during periods of high loads of GitHub Actions runs. ... If there are not enough resources available, the run may be dropped entirely.
> —— [GitHub 官方文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

翻译过来两件事：高负载时**延迟**，资源不够时**整次丢弃**（不是延后，是这次根本不跑）。上游 README 里那句「action 无法准确触发，基本延时 1~1.5h」说的就是前半段。

实测数据（本 fork 2026-08 观测）：

| 现象 | 数据 |
|---|---|
| 正常延迟 | 30~60 分钟 |
| Actions 故障期间 | 4 个调度点丢了 3 个，剩下 1 个延迟 265 分钟 |
| 触发间隔 | 从稳定的 6 小时变成 13 小时、27 小时，时间点随机 |

对签到来说，「今天整个没跑」意味着当天额度作废。所以需要一条不依赖 GitHub 调度队列的触发路径。

## 原理：等于替你按下 "Run workflow"

workflow 里除了 `schedule`，还有 `workflow_dispatch`——就是 Actions 页面上那个 **Run workflow** 按钮。

关键区别在于：

| 触发方式 | 走的路 | 会被丢弃吗 |
|---|---|---|
| `schedule` | 进 GitHub 的**调度队列**，等资源 | **会** |
| `workflow_dispatch` | 直接创建运行 | **不会** |

而 `workflow_dispatch` 有对应的 REST API。让一个外部定时服务每天按点调用这个 API，效果**完全等同于你手动点了那个按钮**——请求一到，GitHub 立刻创建运行，没有排队，没有丢弃。

```
cron-job.org ──POST──> GitHub API ──> 立即创建运行 ──> 签到
  (德国服务器)          (等于按按钮)
```

外部服务只负责「按点发一个 HTTP 请求」，真正的签到逻辑仍然在 GitHub Actions 里跑，账号密码仍然在 GitHub Secret 里，不经过第三方。

## 前置：创建 GitHub 令牌

外部服务要调 GitHub API，需要一个令牌。**用细粒度令牌（fine-grained），只给一个仓库、只给一个权限**，这样即使泄露也没什么可损失的。

1. 打开 https://github.com/settings/personal-access-tokens/new
2. 按下表填：

| 字段 | 填什么 |
|---|---|
| Token name | 随便，比如 `checkin-trigger` |
| Expiration | 想省事就选 `No expiration`；选了具体日期，到期后触发会静默失效 |
| Repository access | 选 **Only select repositories** → 只勾你 fork 的 `anyrouter-check-in` |
| Permissions → Repository permissions → **Actions** | 设为 **Read and write** |

**其它权限一个都不要给。** 配好后 Permissions 那栏应该只显示 Actions 一项。

3. 点 Generate token，把 `github_pat_` 开头那串**立刻复制存好**（页面关掉就再也看不到了）

这个令牌的能力边界：只能让签到 workflow 多跑几次。它读不了代码、改不了代码，也拿不到你的账号密码——**Secret 对令牌是不可见的**。而且脚本本身会跳过当天已领到额度的账号，所以别人拿去乱点也不会造成任何影响。

## cron-job.org 配置

注册 https://cron-job.org （免费），点 **创建 cronjob**，按下面逐字段填。**没提到的字段保持默认即可**。

```
── 基本 ──────────────────────────────────────────
标题:            AnyRouter + AgentRouter 签到
网址:            https://api.github.com/repos/<你的用户名>/anyrouter-check-in/actions/workflows/checkin.yml/dispatches
                 ⚠️ 输入框预填的 http:// 要整个删掉再粘贴，否则变成 http://https://...
激活任务:        ✅ 勾（不勾等于建了个不会跑的任务）
保存响应:        ✅ 勾（以后排查全靠它）

── 运行计划 ──────────────────────────────────────
运行计划:        选最后那个「自订」，上面的每15分钟/每天/每月都不要选
Crontab:         0 9,21 * * *     # 每天 9:00 和 21:00 各一次
调度到期:        留空（那是让任务到期自动停止的，我们要长期跑）
时区:            Asia/Shanghai（在页面下方，默认可能是欧洲时区，务必确认）
                 填完看「下一次运行」预览应变成 9:00 AM；还显示每15分钟说明「自订」没选中

── 通知 ──────────────────────────────────────────
运行失败:        ✅ 勾，Notify after 1 failure
失败后恢复:      ✅ 勾
因失败过多被停用: ✅ 必须勾 —— cron-job.org 连续失败会自动禁用任务，
                 不勾就会不知不觉断签
TLS 证书到期:    ❌ 不勾（那是 GitHub 的证书，轮不到我们操心）

── 认证与请求 ────────────────────────────────────
需要 HTTP 身份验证: ❌ 不勾，用户名密码留空（那是 Basic Auth，和下面的令牌打架）
请求方法:        POST   ⚠️ 默认是 GET，不改会 404
请求本体:        {"ref":"main"}
                 （main 是你要跑的分支名，改过分支名的填自己的）
请求超时:        30 秒（默认）
3xx 视为成功:    ❌ 不勾（我们要的成功码是 204）

标头: 点四次「添加」，每条分「键」和「数值」两栏填
  键 Accept                数值 application/vnd.github+json
  键 Authorization         数值 Bearer github_pat_你的令牌
  键 X-GitHub-Api-Version  数值 2022-11-28
  键 Content-Type          数值 application/json
  ⚠️ Authorization 必须是 Bearer + 一个空格 + 令牌，漏了会 401
  ⚠️ Content-Type 不加也能过（GitHub 宽容），但 cron-job.org 默认发的是
     application/x-www-form-urlencoded，与实际的 JSON body 不符，加上更保险
```

**一个任务就够。** `0 9,21 * * *` 里的 `9,21` 表示这一条每天跑两次，不用建两个任务。

Crontab 五个位置从左到右是 **分 时 日 月 星期**，`*` 表示「每个」：

```
0        9,21      *      *      *
分       时        日     月     星期
第0分    9点和21点  每天   每月   不限
```

## 验证

保存后点 **Test run**，看返回码：

| 返回码 | 含义 |
|---|---|
| **204** | ✅ **成功**。GitHub 这个接口成功时不返回任何内容，别当成失败 |
| 404 | 请求方法还是 GET，或者仓库路径/分支名写错 |
| 401 | 令牌无效，或漏了 `Bearer ` 前缀（注意那个空格） |
| 403 | 令牌权限不对，检查 Actions 是不是 Read and **write** |
| 422 | 请求本体写错，或 `ref` 里的分支不存在 |

看到 204 后，去仓库的 Actions 页面确认——应该在同一秒出现一次新的运行。

也可以先用命令行验证，排除是 cron-job.org 配错了还是令牌本身有问题：

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer github_pat_你的令牌" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/<你的用户名>/anyrouter-check-in/actions/workflows/checkin.yml/dispatches \
  -d '{"ref":"main"}'
```

返回 `HTTP/2 204` 即成功。响应头里的 `x-accepted-github-permissions: actions=write` 可以确认令牌权限刚好够用、没有多余授权。

## 常见疑问

**这样一天会触发好几次，不会封号吗？**

不会。**触发次数 ≠ 签到次数。** 脚本用 `checkin_state.json` 记着每个账号今天有没有到账，后续运行读这个文件就知道该跳过，**不会向平台发任何请求**。从耗时能一眼看出来：真签到要几分钟（启浏览器、逐个登录），跳过的运行 40 秒左右就结束了。所以无论触发几次，**每个账号每天只登录一次**，和你手动领的频率完全一样。

**GitHub 自带的 `schedule` 还要留着吗？**

建议留着。公开仓库的 Actions 免费不限量，多出来的运行都是几十秒的空转，不花钱也不碰平台。留着的好处是两条路径互为备份：cron-job.org 挂了有 GitHub 兜底，GitHub 调度出故障有 cron-job.org 兜底。删掉就等于把签到押在一个免费第三方服务上——而 cron-job.org 连续失败还会自动禁用任务。

**两边同时触发会不会重复签到？**

不会。workflow 里配了 `concurrency` 组，同一时间只允许一次签到在跑，撞上了后到的排队。就算真跑了两次，第二次也会因为「今天已到账」全部跳过。

**为什么不用 Cloudflare Workers / 别的服务？**

都可以，原理一样——任何能定时发 HTTP 请求的服务都行。cron-job.org 的好处是免费、不用写代码、有失败通知。如果你已经有服务器，直接在上面配一条系统 crontab 调用上面那段 curl 也是一样的。
