# yuutils

`yuutils` 是从 [`shell_my`](https://github.com/YuleZhang/shell_my) 独立发布的
Shell 与设备工具合集。它包含 `adb-sync`、可替换 `adb` 前端、SIQ benchmark
工具链、堆栈解析工具、Herdr 会话恢复监控以及共享的 `myenv` 配置。

## 安装

日常安装直接使用现有的作者 pip server，无需克隆仓库，也无需在客户端启动服务：

```bash
python3 -m pip install --upgrade "yuutils==1.5.1" \
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
和 `pyte>=0.8.2,<0.9`（PTY 屏幕解析），pip 自动安装。yuutils 1.5.1 要求 Python
3.8 或更高版本。正式发布产物提供 `SHA256SUMS`，从 GitHub 手动下载时应先核对校验和。

GitHub Release 可作为备用下载入口：

- <https://github.com/YuleZhang/shell_my/releases/tag/yuutils-v1.5.1>

下载 wheel 后可以离线安装：

```bash
python3 -m pip install ./yuutils-1.5.1-py3-none-any.whl
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

## 会话恢复监控（1.5.1）

一行安装并开启：

```bash
# Herdr 终端（当前缺少 UID 证据时仅启动监控，不发送输入）
python3 -m pip install -U 'yuutils==1.5.1' --extra-index-url http://10.168.248.83:8080/simple/ --trusted-host 10.168.248.83 && stepcode-claude-watch.py start
# 普通 SSH 交互终端
python3 -m pip install -U 'yuutils==1.5.1' --extra-index-url http://10.168.248.83:8080/simple/ --trusted-host 10.168.248.83 && step-watch
```

SSH 需要 Linux `/proc`、交互 TTY（远程调用用 `ssh -t`），以及 PATH 中已安装、
已配置的 `stepcode` 或 `stepgo`。只启动自己的新 PTY，不接管已运行的其他 agent：

```bash
step-watch --cli stepgo -- --model <已配置模型>
stepcode-claude-watch.py run --cli stepcode -- --model <已配置模型>
```

显式 `--cli` 优先，否则先找 `stepcode` 再找 `stepgo`。仅显示程序名和启动入口，
不打印参数。通过所选 CLI 的 `--help` 检测接口：StepGo（包括 `stepcode` 符号
链接）使用 `<CLI> agent run stepcli <参数>`；独立旧版 StepCode 仅在识别到
`Usage: stepcode [options]` 时直接启动；未知接口拒绝启动。`--` 后只传 agent
参数，不重复写 `agent run stepcli`。429 使用同一所选 CLI 的 `usage`，当前用户
正常凭据配置由 CLI 自己读取，不复制凭据、不切换账户。

只对当前可见屏幕中的明确重试耗尽过载或 429 `rate_limit_exceeded`，且光标在空白
`Ask Step to do anything (/ for commands)` composer，连续 3 秒无输入/输出时发送
一次 `continue`。429 还需至少一项可识别正余额；未知余额/查询失败跳过，60 秒后
可重新查询。恢复冷却 60 秒，同一错误屏幕不重复提交。输入、粘贴、终端响应和窗口
改变取消候选，未提交的用户输入不重新武装。草稿、审批、工具输出、错误历史或未知
界面不注入。ANSI 刷新用 `pyte` 解析当前屏幕，不扫描历史字节。

Herdr 入口保留（查看日志需系统 `tail`）：

```bash
stepcode-claude-watch.py start
stepcode-claude-watch.py status
stepcode-claude-watch.py log -n 30
stepcode-claude-watch.py stop
```

Herdr 每 5 秒扫描，保留 Claude 504 和 StepCode 恢复代码，但 **Herdr 0.9.3 当前
agent/pane metadata 不提供可靠的服务与目标 UID 证据，恢复失败关闭，不发送按键
或 continue**。自行报告的 UID/PID、pane 名称、可见性不是授权证据；需未来经过
验证的所有权接口才可启用。状态位于系统用户数据库对应 home 下的
`~/.local/state/stepcode-claude-watch/{watch.pid,watch.lock,watch.log}`；目录 0700、
文件 0600，拒绝符号链接、跨 UID、非私有文件。新版本不读取旧平铺状态文件；升级
前需使用旧版命令停止原 watcher。

限制与隔离：

- 使用真实 UID/EUID 和 Linux loginuid，不用 `$USER` 授权；拒绝 sudo、UID/EUID
  不一致或可识别的跨 UID 登录。无法验证用户/目标身份时不注入、不发信号。
- SSH 只写自己持有的 PTY master；清理仅自己的 UID/start-time 匹配子进程组，不用
  pkill、TIOCSTI 或其他 TTY。其他实例不受影响；脱离该组的后代不追杀。
- 透传键盘、终端查询响应、窗口尺寸和退出码；退出/信号恢复终端并清理自己进程组。
  SSH 断开即退出，不保证后台继续。它不是进程沙箱，不阻止 CLI 自身跨权限操作。
- 启发式仅支持上述 StepCode 布局。窄窗口换行、未知 footer/光标、多行 composer、
  频繁重绘可能安全地漏恢复；输入检查与写入之间仍存在不可消除的调度竞争。
- 自动 continue 继续既有 agent 工作，仍受 agent 授权约束。SSH 不落盘屏幕或模型
  输出；不要共享含敏感内容的 CLI 屏幕或 Herdr 日志。

开发验证（假 CLI，不访问模型；临时目录安装，不构建发布 wheel/sdist）：

```bash
python3 -m unittest discover -s yuutils -p 'test_*.py' -v
```

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
