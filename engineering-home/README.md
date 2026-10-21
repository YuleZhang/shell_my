# 工程快捷台 · Engineering Home

`shell_my/engineering-home` 是个人工程快捷入口，不是聊天控制台，不替代 `herdr`。

## 四项能力

- **我的 MR**：按 GitLab 本人 author 查询、状态/项目/日期筛选、历史与分页，跳转原 MR。只读，不合并/审批/推送。
- **API 账户**：读取既有本机 StepCode Key 来源，编辑别名、用途标签（如 `code-review`）、备注及启用状态；直接查询每个预算池余额，有缓存/超时/过期提示。标签不是路由或消费归因。
- **周报**：复用已有 `weekly-report` skill 采集证据，审核后一次性生成；支持编辑、复制、下载和任务历史。无流式对话、不自动发布飞书、不自动重复付费。
- **服务目录**：当前 UID 的 `systemctl --user` service/socket、监听进程、rootless Docker 发布端口，加手工入口；说明、分类、收藏与协议可编辑。无端口和失败服务也保留。不管理服务启停。

## 本机启动

需要 Linux、Node **24+**、npm；周报采集另需 Python **3.10+** 和现有 skill。

```bash
npm --prefix engineering-home ci
npm --prefix engineering-home run build
```

用以下本机命令创建访问配置。密码不进入 argv/历史，不会回显；已有配置拒绝覆盖。不要将真实凭据写在仓库的 `.env` 中。

```bash
read -r -s -p '访问密码（至少12字符）: ' EH_PASSWORD; echo
printf '%s\n' "$EH_PASSWORD" | npm --prefix engineering-home run config:init
unset EH_PASSWORD
EH_CONFIG_FILE="$HOME/.config/engineering-home/config.json" npm --prefix engineering-home start
```

浏览器打开 **http://localhost:4310**（不是 `127.0.0.1:4310`，Host 必须与配置一致）。初始化只创建访问认证和服务发现配置，不导入 provider 凭据、不调用模型。

编辑 `~/.config/engineering-home/config.json` 接入 GitLab、StepCode 和周报，格式见 [配置参考](docs/configuration.md)。服务端配置文件必须是当前 UID 所有、`0600`，不能是符号链接；运行目录必须在仓库外。

## 数据与安全边界

| 内容 | 位置 |
| --- | --- |
| 访问认证、provider/skill/project 配置 | `~/.config/engineering-home/config.json` |
| 既有 StepCode Key（只读） | `~/.stepcode/local-key.txt` |
| 元数据、余额缓存、job、周报及版本 | `~/.local/share/engineering-home/home.sqlite` |
| 源码、锁文件、无密钥示例 | 本仓库 `engineering-home/` |

密钥不会返回浏览器、进入 URL、前端持久存储或正常日志。已有 Key 文件要求 `0600`，父目录不可由其他用户写入；权限不安全会报 `credential source unavailable`，不会自动改写该文件。新增/轮换 Key 在本机原凭据来源完成，网页仅管理标记，不显示或导出明文。原 CLI 账号名与显示别名分离，`sclaude <user>` 不受别名编辑影响。

敏感目录不靠 `.gitignore` 隔离：默认物理保存在 repo 外，数据库/WAL/备份也不得提交。标签、备注、周报可能包含业务信息，备份同样按敏感文件处理。不能承诺防御已被攻陷的同 UID/root。

## 使用顺序

1. 登录；首页只读取缓存，各模块独立失败，不调用 provider 或模型。
2. MR/账户/服务页点击对应刷新按钮，显式更新缓存。筛选变更后可先看缓存，再从 GitLab 刷新；分页遵循服务器返回值。
3. 账户标记仅修改本机元数据。余额保留每个原始桶，不跨币种/重复预算池相加；`未提供` 不等于 0。
4. 周报选择允许项目、日期、零碎记录和上周 Todo，同意扫描。脚本会扫描已有 CLI 会话，默认不保存摘录；脱敏不保证消除所有业务敏感信息。
5. 审核证据及警告，选择已配置模型/账户、输出上限，确认数据和费用后生成。生成是一次无工具模型请求。未知数字/验证声明会提示警告，仍需人工核实。
6. 正文编辑后保存，再复制/下载。远程 HTTP 的剪贴板可能受限，有选中文本手动复制降级；推荐 HTTPS。

重启前尚未发送的生成任务成为 `awaiting_resume`，必须重新确认原模型/账户、当前输入规模和预算。`sending` 结果未知时保留 `outcome_unknown`：供应商可能已扣费，不自动重发；只有明确接受重复计费风险才创建新的重试任务。任务历史刷新/查看不是恢复操作。重新生成的结果若遇到并发编辑会保留为备选稿，不覆盖正文。

## 开发、验证与部署

```bash
npm --prefix engineering-home run lint
npm --prefix engineering-home run typecheck
npm --prefix engineering-home test
npm --prefix engineering-home run build
```

生产是一个后端进程服务构建后的网页。源码热更新：把本机配置的 `publicOrigin` 设为 `http://localhost:5173`、后端端口保留 4310，分别执行：

```bash
EH_CONFIG_FILE="$HOME/.config/engineering-home/config.json" npm --prefix engineering-home run dev:server
EH_DEV_BACKEND_PROXY=http://localhost:4310 npm --prefix engineering-home run dev
```

仅明确设置 loopback 目标才开启 Vite API proxy；它保留 Host，不绕过鉴权。浏览器使用 `http://localhost:5173`。开发修改生产配置前请保留本机私有备份。

离线浏览器回归见 `web/tests/browser_check.py`：需安装 Selenium、Firefox 和 geckodriver。先启动 `npm --prefix engineering-home run dev -- --port 5177`，再用安装了 Selenium 的 Python 执行检查。fixture API 拦截真实请求，不调用模型。若服务器文件监听上限耗尽，可设置 `CHOKIDAR_USEPOLLING=true` 启动开发服务，不需改系统配置。

完整静态 UI + 实际鉴权/API 的 fixture 检查：构建后在应用子目录执行 `node_modules/.bin/tsx tests/browser-server.ts`，另一个终端执行 `python3 web/tests/full_browser_check.py`。仅 loopback 4319、临时数据库、模拟凭据和 provider，不使用真实账号。检查结束手动停止临时 server。

systemd 用户服务模板见 [deploy/engineering-home.service](deploy/engineering-home.service)，安装/启用需单独决定。远程访问请使用 HTTPS 反向代理或 SSH tunnel，并确保浏览器 Origin/Host 与 `publicOrigin` 一致；不允许公网裸露、不自动安装服务、不改其他服务。

[配置与故障说明](docs/configuration.md) · [架构与验证边界](docs/design.md)
