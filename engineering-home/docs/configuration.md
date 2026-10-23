# 本机配置参考

通过 `config:init` 创建 `~/.config/engineering-home/config.json` 后，在服务器本机编辑。以下只描述结构，**不提交真实配置**。所有路径为绝对路径；endpoint 必须 HTTPS 或可信 loopback HTTP，不可带用户名、密码、query 或 fragment。修改后重启应用生效。

## 基础字段

- `publicOrigin`：浏览器实际使用的精确 origin，例如 `http://localhost:4310` 或你的 HTTPS 入口；不带末尾 `/`。
- `listenHost`：默认 `127.0.0.1`，`port` 默认 4310。反代同机时保持 loopback。
- `auth.passwordHash`：scrypt 密码摘要，由初始化生成；`auth.cookieSecret` 为随机私有值，不是 API Key。
- `auth.secureCookie`：HTTPS 用 `true`；只有 localhost/loopback HTTP 才允许 `false`。
- `projectRoots`：`[{"id":"project-id","label":"项目名称","root":"/absolute/repo/path"}]`。周报只能选择这些项目，id 必须稳定且唯一。

## StepCode

添加 `stepcode` 对象：

```json
{
  "baseUrl": "https://YOUR-APPROVED-STEPCODE-HOST",
  "concurrency": 4,
  "timeoutMs": 4000,
  "batchTimeoutMs": 20000,
  "ttlMs": 60000
}
```

`baseUrl` 是提供 `/desk/v1/stepcode/user/info` 的根地址，不是模型的 `/v1/chat/completions`。凭据只从既有 `~/.stepcode/local-key.txt` 读取，不从网页导入、不自动复制。支持原来的 `user key` / `user: key` 与注释格式。每个账户的缓存以 endpoint 和私有凭据指纹隔离；同名轮换后旧余额不会被当作新余额。

## GitLab

添加 `gitlab` 对象，`baseUrl` 为 GitLab 根地址，`token` 在私有配置中由你本机填写。建议最小化 `read_api` 权限。不要把真实 token 放在本文、仓库 fixture 或命令 argv 中。

- 默认通过 `/api/v4/user` 识别本人，查询 `scope=all + author_id`。
- 如明确使用 bot 凭据代表本人，需要配置真实本人 `authorId`，并设置 `botBindingExplicit: true`；否则拒绝把 bot 当本人。
- `timeoutMs` 默认 5000，`ttlMs` 默认 60000；可选 `headers` 仅允许 `X-*` 和 `User-Agent`，不得作为任意请求代理。
- 项目过滤用项目 MR API，未过滤用全局 API；分页由 GitLab headers 决定。无权限/无分页信息会明确标记，不伪称 MR 不存在。

## 周报

添加 `reports` 对象：

```json
{
  "skillPath": "/absolute/custom-harness-engineer/skills/weekly-report",
  "python": "/usr/bin/python3.10",
  "timeoutMs": 120000,
  "maxBytes": 4000000,
  "models": []
}
```

`models: []` 可只采集/编辑，生成不启用。skill 必须包含 `scripts/weekly_commits.py`、`references/style-guide.md` 和 `references/template.md`。规则从当前 skill 读取并记 hash，不复制另一份规则；不调用该 skill 的飞书发布阶段。

配置模型条目后生成才可用，例如以下**无真实密钥**的结构：

```json
{
  "id": "weekly-model",
  "label": "周报模型",
  "endpoint": "https://YOUR-APPROVED-MODEL-HOST/v1/chat/completions",
  "model": "YOUR-ALLOWED-MODEL-ID",
  "useStepcodeAccount": true,
  "maxOutputTokens": 4000,
  "maxInputChars": 60000,
  "timeoutMs": 60000
}
```

`endpoint` 是完整 OpenAI-compatible chat-completions URL。`useStepcodeAccount: true` 时从已启用账户选择 Key；必须明确批准模型 host，不能把 StepCode 凭据发给任意供应商。若使用供应商自有凭据，设置 `false`，本机填写 `apiKey`，网页不返回该值。客户端不能自定义 endpoint/model；输出 token 上限不等于金额预算，不承诺精确费用。

## 服务目录

`services.enabled` 默认 `true`，`ttlMs` 默认 15000。`services.dockerCommand` 默认 `/usr/bin/docker`；若 Docker CLI 安装在用户目录，请填 `command -v docker` 得到的绝对路径。这是服务器本机配置，网页不能修改执行路径。systemd 模板 unit 保留为“未实例化”，部分详情不可用时保留占位而不丢失其他记录。

可设置 `accessAddress` 为服务器已确认的具体内网/Tailscale IP，为 wildcard 监听生成候选访问链接；保持原监听地址，标注远程可达性未验证。IPv4/IPv6 地址族不匹配时回退 loopback，不改写真正的 localhost-only 服务。

网页可编辑协议/path/备注/分类/收藏。只有显式配置协议才提供链接，不凭端口猜 HTTP。`services.manual` 可补充固定入口（仅 IP、port、协议、路径，无主动扫描）：

```json
{
  "id": "docs",
  "name": "内部文档",
  "address": "192.0.2.10",
  "port": 8080,
  "protocol": "http",
  "path": "/"
}
```

示例 IP 属于文档保留网段，不是可用服务。自动发现仅当前 UID；rootless Docker 使用 `/run/user/<uid>/docker.sock` 并验证身份。缺少 Docker 不影响其他来源，部分来源失败保留旧快照并提示过期。

## 常见问题

- `invalid host/origin`：浏览器入口与 `publicOrigin` 不一致，或反代改写 Host。保持精确 origin/Host，不关闭校验。
- `credential source unavailable`：本机 Key 文件缺失、格式错误、权限不安全或是 symlink。按原 CLI 来源本机处理；应用不会打印其内容。
- `generation disabled`：未配置模型或当前 skill 不可读。已存周报仍能查阅和编辑。
- `awaiting_resume`：重启前未发送的生成暂停；重新确认原绑定与当前输入，只恢复指定任务。
- `outcome_unknown`：请求可能已经收费；不能自动重发，显式确认重复计费风险后创建新任务。
- 不做自动健康探测；服务 running/listening 不等于网页健康或从客户端可达。
