# Codex App 代理部署

在安装了 Codex App 的 Linux 主机上，先确保 HTTP 上游代理可用，然后在克隆的仓库中运行：

```bash
./codex_proxy/install.sh
```

默认上游是 `127.0.0.1:17890`。其他地址可在部署时指定：

```bash
MIHOMO_UPSTREAM_HOST=proxy.example.com MIHOMO_UPSTREAM_PORT=8080 ./codex_proxy/install.sh
```

脚本在缺少 mihomo 时下载并校验固定版本，安装两个用户级 systemd 服务，验证路由配置并启用服务。已有的不同配置会先备份到原目录。Codex updater 使用本机 `127.0.0.1:17891`：OpenAI 和 Anthropic 域名转发到指定的 HTTP 上游代理，其他流量直连。上游代理及其认证由使用者自行提供；此配置只支持无需认证的 HTTP 上游。

安装器还会完成以下容易遗漏的步骤：

- 将代理变量写入 `~/.config/codex-proxy/env.sh`，并从 `~/.profile`、`~/.bashrc` 和 `~/.zshenv` 加载，使新 SSH 登录的 Codex CLI 继承代理。
- 将相同变量写入 `~/.config/environment.d/80-codex-proxy.conf` 并导入 systemd 用户环境。
- 显式重启 Codex app-server daemon；仅重启 updater 不会替换已经运行且仍保留旧环境的 daemon。
- 读取当前 ChatGPT 账户的模型目录。如果 `~/.codex/config.toml` 顶层强制指定了账户不支持的模型，先备份配置，再移除该项，让 Codex 使用服务端默认模型。
- 通过代理执行一次到 `api.openai.com` 的 HTTPS CONNECT，避免只检查端口监听却遗漏上游不可用。

部署后检查：

```bash
systemctl --user status mihomo-codex-egress.service codex-app-server-updater.service
curl -I --proxy http://127.0.0.1:17891 https://api.openai.com/
codex debug models
codex exec --skip-git-repo-check PROXY_OK </dev/null
```

模型列表由当前登录的 ChatGPT 账户下发。把不在 `codex debug models` 输出中的模型名写进 `config.toml` 不会增加模型权限，只会在请求阶段得到“不支持该模型”的错误。

## 切换 ChatGPT 账户

Codex CLI 使用共享的后台 app-server daemon。执行退出和重新登录后，已经运行的 daemon 可能仍保留旧账户状态，因此切换账户时应显式重启：

```bash
codex logout
codex login --device-auth
codex app-server daemon restart
codex login status
```

如果登录文件的更新时间晚于 daemon 的启动时间，而 TUI 仍显示旧账户，通常就是 daemon 尚未重新加载凭据。不要删除 `auth.json`；正常重启 daemon 即可。

开发者可在 Linux 上运行隔离测试：

```bash
./codex_proxy/test_install.sh
```
