# 架构与验收边界

## 为什么是薄应用

Vite/React 只做个人仪表板；Fastify 同源服务静态产物和固定 API；Node 24 的 `node:sqlite` 保存本机元数据、缓存、报告版本和任务。没有 agent runtime、聊天、终端代理、Redis、自动发布或服务控制。

参考 Mission Control 的状态/产物组织、CloudCLI 的工程入口和服务门户的分类交互，但未 fork 其执行器/素材。原 `herdr`、`sclaude`、FlowForge 及外部周报 skill 不修改。

## 模块边界

- `shared/contracts.ts`：浏览器可用 DTO 和输入 schema，不导入 Node 或凭据。
- `server/security/`：私有文件权限、精确 Host/Origin、cookie/CSRF、脱敏与安全错误。
- `server/adapters/`：GitLab、StepCode、当前 UID 服务、外部周报证据脚本、无工具模型请求。
- `server/jobs/runner.ts`：持久化一次性工作，发送前落库，结果未知禁止自动再付费。
- `server/store.ts` + `migrations/`：SQLite，单消费者锁、版本化报告，数据位于 repo 外。
- `web/src/pages/`：总览、MR、账户、服务和周报；独立缓存读取与错误状态。

读取 API 不刷新外部网络或调用模型。用户显式刷新才读 GitLab/用量/服务；用户同意采集才读已有 Git/会话；用户确认输入及费用才发送模型。

## 隐私与恢复

GitLab/model 凭据来自本机私有配置；StepCode 来自既有只读 Key 文件。脱敏在持久化、预览和模型发送前执行，包含 quoted/nested JSON、常见 Key/token 和已配置秘密。脱敏不是信息泄露的绝对保证：会话摘录默认关闭，用户需审核内部数据是否允许送供应商。

预算保持原始桶，未知数值是 null，不跨池合计。凭据轮换和 endpoint 改变隔离缓存。MR 使用本人数字 author，bot 需显式绑定。

服务只枚举当前 UID，无网段探测、环境读取、完整 Docker inspect 或通用 URL proxy。监听地址与访问 URL 分开；手工 protocol/IP 配置只生成候选链接，不证明可达。

报告采集、生成分别创建幂等 job。重启后未发送生成成为 `awaiting_resume`，只有指定任务的重新确认才恢复。`sending` 变 `outcome_unknown`，不自动再发；不能保证供应商“恰好一次收费”。正文版本冲突保留本地编辑，生成期间的并发编辑会保存备选稿。模型格式/无证据数字会提示 WARN，人工审核不可省略。

## 验证方式

- Node tests：认证/CSRF/Host、权限和秘密、GitLab identity/pagination、余额桶/缓存、UID/Docker/IPv6、证据范围、报告版本/恢复/重复收费。
- 前后端 TypeScript + ESLint + Vite production build。
- Selenium/Firefox 的离线 fixture 交互：job rediscovery、明确恢复、重复计费确认、备选稿、移动端和复制降级；不依赖真实 provider。
- SQLite 与 fixture 为临时私有文件；测试截图/日志忽略，真实报告/Key 不做 fixture。

真实 GitLab、余额接口和付费模型的端到端效果必须在本机私有配置完成后单独验证；mock/fixture 通过不等于真实供应商兼容已证实。首版不自动安装 systemd、不合并 PR、不发布飞书。
