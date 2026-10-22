# yuutils

`yuutils` 是从 [`shell_my`](https://github.com/YuleZhang/shell_my) 独立发布的
Shell 与设备工具合集。它包含 `adb-sync`、可替换 `adb` 前端、SIQ benchmark
工具链、堆栈解析工具、Herdr 会话恢复监控以及共享的 `myenv` 配置。

## 安装

日常安装使用现有作者 pip server，无需克隆仓库或启动新的 agent 对话：

```bash
python3 -m pip install --upgrade "yuutils==1.5.2" \
    --extra-index-url http://10.168.248.83:8080/simple/ \
    --trusted-host 10.168.248.83
```

检查安装结果：

```bash
yuutils
adb-sync --version
run_siq_benchmark.py --self-test
```

该服务仅用于受信的内部网络；不要在不可信网络中使用明文 HTTP 安装。由于服务是
HTTP 索引，必须传入 `--trusted-host`。客户端需要能够访问 `10.168.248.83:8080`；
无法访问时请联系作者确认当前网络地址。Python 运行时依赖为 `tabulate`（性能表格）
和 `pyte>=0.8.2,<0.9`（PTY 屏幕解析），pip 自动安装。yuutils 1.5.2 要求 Python
3.8 或更高版本。正式发布产物提供 `SHA256SUMS`，从 GitHub 手动下载时应先核对校验和。

GitHub Release 可作为备用下载入口：

- <https://github.com/YuleZhang/shell_my/releases/tag/yuutils-v1.5.2>

下载 wheel 后可以离线安装：

```bash
python3 -m pip install ./yuutils-1.5.2-py3-none-any.whl
```

## SIQ benchmark 工具链

1.5.0 将以下三个命令和所需 Python 依赖一起安装：

| 命令 | 用途 |
| --- | --- |
| `run_siq_benchmark.py` | 构建、部署并运行原生 `siq_test`，收集内存与性能产物 |
| `process_sdk_log.py` | 统计日志中的 `leave <function> <n>ms` 数据 |
| `scripts_merge_mpool_csv_to_perfetto.py` | 将内存 CSV 计数器合并到 Perfetto trace |

典型用法：

```bash
run_siq_benchmark.py path/to/scripts/run_<project>.sh \
    --adapter <adapter_name> \
    --input-dir path/to/input \
    --output-dir path/to/output
```

运行前还需要：

- `adb`、`git`、`bash` 以及常见 Linux 命令（`cat`、`find`、`rm`、`seq`、
  `sha256sum`、`wc`）可用；
- Android 设备已连接且对应 pipeline 的 `run_*.sh` 可独立运行；
- 输入目录、工程参数和设备权限已经准备好。

benchmark 的工作目录会保留命令审计信息以及底层脚本的完整标准输出。工具会对常见
凭据环境变量做脱敏，但底层脚本仍可能自行打印敏感内容；共享 `run.log`、`debug.log`
或完整结果目录前应先检查并脱敏。

`run_siq_benchmark.py` 会自动定位同一 yuutils 安装中的
`process_sdk_log.py`、Perfetto 合并脚本和 `adb-sync` 环境。也可以显式覆盖：

```bash
run_siq_benchmark.py path/to/run_project.sh \
    --process-sdk-log /path/to/process_sdk_log.py \
    --merge-script /path/to/scripts_merge_mpool_csv_to_perfetto.py \
    --adbsync-env /path/to/adbsync-env.sh
```

不使用 `adb-sync` 时传入 `--no-adbsync`。仅检查本地源码改写逻辑、不连接设备：

```bash
run_siq_benchmark.py --self-test
```

两个配套工具也可以单独运行：

```bash
process_sdk_log.py run.log --functions process_image
scripts_merge_mpool_csv_to_perfetto.py \
    --csv mem_use_info.csv \
    --trace run.perfetto
```

## adb-sync 前端

在当前 shell 中启用 yuutils 提供的 `adb` 前端：

```bash
source "$(adb-sync-env)"
adb push --mirror input_dir/* /data/local/tmp/app/input
source "$(adb-sync-env)" off
```

该前端将 push 操作委托给打包的 `adb-sync`，其他 adb 命令透传给真实 adb。
如需使用独立安装的 adb-sync，可设置 `ADB_SYNC_SCRIPT`。

`adb-sync` 本身仍保持独立版本与测试；yuutils 版本表示本次工具集合快照的版本。

## 会话恢复监控（1.5.2）

1.5.1 默认是前台新 PTY，**不是监控已有会话的后台服务**。1.5.2 默认改为当前 UID
后台 daemon：打印 PID 和监控范围后返回 shell，不创建对话、不打开其他 TTY。
已有未接入的会话需要按下面步骤加载桥接；未验证的版本和后端会显示原因，不会假报可自动回复。

```bash
python3 -m pip install -U 'yuutils==1.5.2' --extra-index-url http://10.168.248.83:8080/simple/ --trusted-host 10.168.248.83 && step-watch start
step-watch status
step-watch log
step-watch stop
```

`stepcode-claude-watch.py start/status/log/stop` 同样委托给后台服务。
启动会安装桥接到当前 `HOME`（去除首尾空白；未设置时回退 passwd home）下
`.stepcode/agent/extensions/yuutils-step-watch` 和 `.stepgo/config/agent/extensions/yuutils-step-watch`。
已有会话需要用户执行一次 **`/reload`**，后续新会话自动加载；daemon 不替用户输入该命令。
自定义目录用 `step-watch start --agent-dir /absolute/agent/root`（可重复）；不会扫描配置或凭据。
已有不同内容的扩展文件不会覆盖，需先人工审阅/移除旧桥接文件再安装，随后 `/reload`。

Linux `/proc`、Unix socket/SO_PEERCRED 和已安装的 native Step 扩展接口必需；后台服务
只使用 Python 标准库，不要求交互 TTY、独立 Node 命令或 Herdr。桥接由 native 内置 JS
运行时执行，使用 `node:fs/path/http/crypto`。`pyte` 仅供显式旧 PTY 模式；`tabulate` 供性能工具。
拒绝 sudo、UID/EUID 不一致、跨登录用户、非私有目录、符号链接与不可信端点。

### 已验证安全范围

- 原生 `agent_end` 的最后 assistant 必须是明确的服务器过载错误；`agent_settled` 后
  空闲、无 pending、空编辑器、无图片占位草稿、无粘贴/输入、无模态/overlay/自动补全，
  且静默 3 秒、恢复冷却 60 秒，才调用固定
  `pi.sendUserMessage('continue', {deliverAs: 'followUp'})`。仅消费加载桥接后观察到的错误，
  不扫描 transcript、旧日志或终端历史；已有历史失败不会自动引导恢复。
- 通过原生 `ui.custom` 的立即关闭 overlay 工厂取得 TUI，只观察真实默认编辑器，
  不另建或替换编辑器。对原生重载时相同文本的重复赋值做保留处理，避免丢失长粘贴展开数据；
  退出时恢复自己的方法包装。每次 IPC 同步重新检查 focus、overlay 和编辑器属性。
  原生本构建图片仅按文本中的 `[Image #n]` 解析；已删除占位的孤立注册项不发送。
  输入取消候选并锁定到下一新回合，以覆盖异步剪贴板读取。
- 仅验证过 SHA256 为
  `da9218ed201419a93cd233adb203743657fe497fee15117e768d2c9438dd3ae5`
  的 Linux native ELF。其他二进制、RPC、SDK、自定义 editor factory 或未知 UI
  显示 `ui-safety-unavailable`，不允许通过用户“verified”标志绕过。
- **429 `rate_limit_exceeded` 暂不自动恢复**：原生上下文未提供可可靠绑定所选 wrapper
  CLI 的账号/凭据配置。不能拿另一 CLI 的 `usage` 正余额冒充同账号；返回
  `balance-unverified`，即使外部声称余额验证成功也拒绝。
- `submitted-unconfirmed` 只表示已经调用发送 API，不表示模型收到或完成；调用前消费候选，
  发送抛错/超时/重复请求不重发，不绕过 agent 的审批。

daemon 每秒发现当前 UID 的已有/未来 `stepcode/stepgo`（包括 Node `stepgo.js`）、native
及通过同 UID `/proc` 祖先链关联的 native 子进程。`status` 区分 `connected`、
`needs-extension-load`、`waiting-child`、`unsupported-backend`；连接不等于允许恢复。
Herdr 本身缺少可靠目标所有权接口，不通过 pane API 注入；其中独立 native 会话只有建立
可信桥接后才按上述规则恢复。`stop` 仅停止 daemon，不关闭任何 agent。

默认私有 runtime 为 `/tmp/yuutils-watch-<UID>`（目录 0700；socket/元数据/运维日志 0600）。
可用 `YUUTILS_WATCH_RUNTIME` 指定同样私有的绝对短路径（最多 65 字节，daemon 与会话需一致）。
鉴权 capability 仅存私有端点元数据，不打印；不记录模型输出、提示、错误正文、完整 cmdline
或凭据。日志只含固定启动/停止信息；进程身份同时检查 PID/start-time、四个 UID 和 peer credentials。

### 显式旧模式

仅需新前台 PTY 时：`step-watch run --cli stepgo -- --model <已配置模型>`。
StepGo 入口由已验证 CLI help 识别为 `agent run stepcli`；未知 CLI 拒绝启动。
该模式不是后台已有会话服务，依赖 `pyte`，SSH 断开会退出。
旧 Herdr 监控用 `stepcode-claude-watch.py herdr start/status/log/stop`（运行需要 PATH 中的
`herdr`、所选 CLI；跟踪日志需要 `tail`），缺少所有权证明仍不发按键。升级前先停止旧进程。

### 开发验证（不访问模型）

```bash
python3 -m unittest discover -s yuutils -p 'test_*.py' -v
node yuutils/test_step_watch_bridge.mjs
python3 yuutils/native_watch_smoke.py --native "$HOME/.stepcode/bin/step"
python3 yuutils/native_watch_smoke.py --tui --native "$HOME/.stepcode/bin/step"
python3 yuutils/validate_watch_package.py
```

JS 检查需 PATH 中的 Node.js。原生测试仅启动新隔离 HOME/TUI；不读真实凭据，
不向现有会话发输入。TUI 测试通过真实 Unix HTTP 服务及 Python SO_PEERCRED 客户端
触发一次原生发送 API，在 native `input` 事件返回 `handled` **阻止模型调用**；
分别验证文字、原生图片占位、未结束粘贴、模态、overlay 和 429 拒绝，不保存终端输出。

## 其他已安装内容

运行 `yuutils` 可以列出所有公开命令。`myenv/zsh` 脚本安装为：

- `install_zsh_env.sh`
- `test_install_zsh_env.sh`

`myenv/omp` 配置安装到 `yuutils-support/myenv/omp`。

## 发布维护

以下操作只需要在作者服务器上执行，普通安装者不需要运行：

```bash
# 修改 setup.py 中的 VERSION 后构建
scripts/serve-pip-index.sh --build-only

# 管理后台索引服务
scripts/pip-index-ctl.sh start
scripts/pip-index-ctl.sh status
scripts/pip-index-ctl.sh restart
scripts/pip-index-ctl.sh log -f
```

`pypiserver` 会在每次请求时重新扫描 `dist/`，所以将新产物放入当前服务使用的
`dist/` 后不需要重启。旧版本应保留；只有明确下线历史版本时才使用 `--clean`。
