# yuutils

`yuutils` 是从 [`shell_my`](https://github.com/YuleZhang/shell_my) 独立发布的
Shell 与设备工具合集。它包含 `adb-sync`、可替换 `adb` 前端、SIQ benchmark
工具链、堆栈解析工具以及共享的 `myenv` 配置。

## 安装

日常安装直接使用现有的作者 pip server，无需克隆仓库，也无需在客户端启动服务：

```bash
python3 -m pip install --upgrade "yuutils==1.5.0" \
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
无法访问时请联系作者确认当前网络地址。`tabulate` 是唯一的第三方 Python 运行时依赖，
pip 会自动安装。yuutils 1.5.0 要求 Python 3.8 或更高版本。正式发布产物同时提供
`SHA256SUMS`，从 GitHub 手动下载时应先核对校验和。

GitHub Release 可作为 pip server 不可达时的备用下载入口：

- <https://github.com/YuleZhang/shell_my/releases/tag/yuutils-v1.5.0>

下载 wheel 后可以离线安装：

```bash
python3 -m pip install ./yuutils-1.5.0-py3-none-any.whl
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
