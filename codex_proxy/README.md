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

部署后检查：

```bash
systemctl --user status mihomo-codex-egress.service codex-app-server-updater.service
curl -I --proxy http://127.0.0.1:17891 https://api.openai.com/
```
