# yuutils

`yuutils` 是从 [`shell_my`](https://github.com/YuleZhang/shell_my) 独立发布的
Shell 与设备工具合集。它包含 `adb-sync`、可替换 `adb` 前端、SIQ benchmark
工具链、堆栈解析工具、Herdr 会话恢复监控以及共享的 `myenv` 配置。

## 安装

日常安装使用现有作者 pip server，无需克隆仓库或启动新的 agent 对话：

```bash
python3 -m pip install --upgrade "yuutils==1.5.3" \
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
和 `pyte>=0.8.2,<0.9`（PTY 屏幕解析），pip 自动安装。yuutils 1.5.3 要求 Python
3.8 或更高版本。正式发布产物提供 `SHA256SUMS`，从 GitHub 手动下载时应先核对校验和。

GitHub Release 可作为备用下载入口：

- <https://github.com/YuleZhang/shell_my/releases/tag/yuutils-v1.5.3>

下载 wheel 后可以离线安装：

```bash
python3 -m pip install ./yuutils-1.5.3-py3-none-any.whl
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

## SIQ 结构化结果与原目录 commit A/B

`--json` 是布尔开关，stdout **恰好一个 JSON 对象**（失败也一样），进度和脱敏错误走
stderr；不会回显原始 `run.log`。例如沿用原运行参数，再追加开关：

```bash
run_siq_benchmark.py path/to/scripts/run_project.sh \
  --adapter adapter --input-dir /absolute/input --serial DEVICE \
  --siq-project project/adapter --mode repeat_full --loops 10 --json > result.json
jq '{schema_version,kind,status,measurement_valid,exit_code,validity,performance,memory,sampling}' result.json
```

结果保留原 summary 字段，增加 `schema_version: 1`、`kind: single|ab`、
`status: ok|warning|failed|interrupted`、`measurement_valid`、`phase`、`exit_code`、
`primary_error` / `recovery_error`，以及 `experiment/source/device/performance/memory/sampling/artifacts`。
`validity` 分开记录原生生命周期、部署、内存、宿主后处理、trace 和源码恢复。
原生完成、部署和 CSV 有效时，宿主或 merge 失败不会抹掉测量数据；merge/helper warning
仍返回非零。仅原脚本 native 完成后的 host exit 非零沿用原退出语义（可返回 0，
但 `status=warning`、`run_exit` 和 `validity.host` 明确记录）。不要仅凭退出码判断测量。

- `performance.<function>` 保留所有选中样本、统计、单位和来源；JSON 统计与比较以这些
  结构化样本为准，legacy 字段保留。每个请求函数记录 `iteration_counts`、
  `iteration_composition`（日志中的调用名称序列）与 `calls_per_iteration`；只有已验证窗口内
  每轮非零且调用构成一致才标 complete，不能用总数整除替代逐轮覆盖。
  repeat_full 以完整 lifecycle 为一轮，marker 后的 release 仍属于该轮；repeat_process 的
  逻辑窗口从 lifecycle BEGIN / 上一个 outer marker 到当前 marker，不等于精确函数进入范围。
  窗口外样本、缺失/无效 lifecycle 标 unverified，缺轮或构成不稳定标 invalid；仍保留样本和 counts。
  `process_image` 完整且逐窗口对应的细粒度 leave 优先，否则回退完整外层 marker；
  无 lifecycle 时可以保留选中的 leave/marker 数据，但不声称覆盖完整。
  首样本不自动删除，stddev 是本次样本的总体标准差，不是独立实验的置信区间。
  仅完整稳定序列生成 call slot；它是推断的调用序号，不代表已证明的 call-site。
- 内存单位保持 producer 的 **MB**。sample mean 来自 `*_cur`，peak 来自累计 max 列
  （缺 max 列时可以使用当前列的采样 max，但来源不同）。joint 指标直接读取同时刻
  DMA+RSS+GPU mmap total，**绝不把独立 peak 相加**；getrusage 单列保留。
  缺失指标是 null/missing，不是零。native Summary 与 CSV 校验允许 0.0051 MB 显示舍入。
- `sampling.configured_period_ms` 与 `actual_interval_ms` 独立；报告样本跨度、重复时间、
  缺失/未知列和质量警告。非有限值、倒序时间、生命周期不完整、native/CSV 严重不符
  拒绝测量。PID 与 native peak 校验不等于全时段覆盖证明；没有对齐的生命周期时间戳时
  `coverage.status=unverified`，不输出虚假的 100% 覆盖率。采样 peak 不是连续时间真 peak。
- `experiment` 记录输入 digest/帧数、循环模式、配置、脚本/tool hash 和共享 build 的证据。
  这是启用内存采样时的计时，不是无插桩纯性能。`source` 在创建产物/插桩前快照，记录
  full HEAD、branch/detached、index/源码 diff 指纹、untracked 摘要、子模块 actual HEAD。
  `device` 只查询 selected serial 与白名单属性，不保存全部 getprop/env。
- 产物目录私有（新运行目录 0700、文件 0600）；raw log 仍可能含秘密，仅供本地检查，
  不应直接上传。日志原文不修改；CSV pull 每次尝试保留独立日志。stdout 的 summary hash
  指向磁盘 `summary.json`；文件不包含自身 SHA-256。旧 `performance_report` 字符串由
  结构化统计生成，原 helper 文本保留在私有 `performance.log`。
  当前部署是扁平目录：install 的 common/libs 与 libs 中出现重复 basename 时，
  即使字节相同也直接拒绝，不静默折叠；build-before 仅允许真正缺产物的空快照。
  设备哈希输出中的重复路径/basename、缺项、额外项或异常格式也不能标 deployment verified。

### A/B 切换及恢复

```bash
run_siq_benchmark.py path/to/scripts/run_project.sh \
  --adapter adapter --input-dir /absolute/input --siq-project project/adapter \
  --mode repeat_full --loops 10 --json --ab-test-commit COMMIT > comparison.json
# --ab_test_commit COMMIT 是同义参数；COMMIT 必须能在本地解析为 commit。
jq '.comparison | {direction,checks,metrics}' comparison.json
```

仅在**原 worktree** 运行：目标 detached A → 恢复原 branch/full HEAD/index/源码 → 当前 B。
不创建 worktree/clone/branch，不使用 force/reset/clean，不篡改 run 脚本，不删除用户 build/cache。
单次与 A/B 共用原 `siq-benchmark.lock`，入口一次持锁，内层不自锁。
普通主仓库 tracked staged/unstaged 修改通过事务专属 **tracked-only stash** 保存；
不 stash untracked/ignored，不 stash 子模块。tracked dirty 子模块、冲突、进行中的 Git 操作、
staged gitlink 修改及第一层 merge/rebase/custom/none 子模块 update 策略直接拒绝。
checkout 关闭隐式 submodule recurse，并使用 `--no-overwrite-ignore`；子模块 update 仅为
`git submodule update --init`（不加 recursive），更新前检查 ignored/untracked 碰撞，更新后核验第一层实际 HEAD。
快照沿实际 HEAD tree 的 gitlink 边界递归记录已有子模块 HEAD、branch、tracked fingerprint 和初始化状态，
不扫描 untracked repositories。允许非递归更新不触动的嵌套子模块；已有 initialized nested 身份必须保持不变，
且目标 A 中 initialized nested actual 必须匹配目标 gitlink，否则明确失败，不继续测量 A，也不补 recursive update。
未初始化 nested 明确记录为 uninitialized，不主动 init；已知未初始化 nested 的目标 gitlink 改变也保守失败。
目标新增 nested gitlink 不得借用已有非空/untracked repository。未初始化且非空的第一层子模块目录及
无法在本地检查的已有子模块目标对象仍会拒绝，不通过强制操作绕过保护。

恢复时原 root/各级子模块 branch OID 不得移动；按 root、父子模块、子子模块顺序恢复原身份，
已有子模块 actual HEAD 即使不同于原 gitlink 也会恢复，并递归核验 HEAD/branch/index/worktree 指纹。
原未初始化 nested 保持未初始化；新初始化的第一层依赖保留并记录，不自动 deinit/删除。
stash 用精确 OID `apply --index`，核验恢复后**仍保留本次 stash**，旧/新用户 stash 都不删除。
仓库 advisory lock 无法排除外部 Git 并发，不能证明 ref/reflog 的原子删除安全；不直接重写 Git ref/reflog，
不调用基于 `stash@{0}` 索引的 drop。receipt 的 `stash_dropped=false`、`stash_retained_reason` 以及
result warnings 明确记录此保留理由；`source_restore=verified` 与 stash 保留互不冲突。
apply 冲突可能部分应用，停止且不重试。检测到 A 改写 tracked 源码/缓存、恢复冲突或并发
branch 移动时停止，**不运行 B**。锁不能阻止外部手动 Git 操作/源码编辑；运行期间不要这样操作。

私有 `ab_transaction.json` 记录原身份、阶段、完整 stash OID；持久入口 receipt 位于
`git rev-parse --absolute-git-dir` 对应的 `siq-benchmark-transaction.json`。stash 前先写
pending、旧 stash ref 和唯一事务标识，防止在 stash 成功但 OID 尚未落盘时猜测恢复。
SIGINT/TERM/timeout 会终止子进程组并尝试恢复，primary 与 recovery 错误分开；SIGKILL/断电
不能执行 finally。下次发现未完成 receipt **直接拒绝**，不要盲删 receipt、不要反复 apply；
先按 receipt/patch/stash OID 人工确认源码、index、branch、子模块与恢复状态。
执行层写入的 `active_artifacts` / `patch_receipt` 指向最近一侧的恢复凭据；阶段保存会在
相同 transaction_id 下合并这两个非空字符串字段，A 测量后、恢复中断时和 B 完成后均保留。
阶段/stash/原身份仍由事务所有者控制，不全量合并磁盘内容；新事务不继承旧指针，运行中
receipt 身份变化则拒绝覆盖。正常和恢复失败的最终归档 `ab_transaction.json` 均刷新执行指针；
若刷新无法核验或写入，保留原 primary/recovery 错误并标记 execution_pointers_verified=false，
不把上一侧的缓存指针冒充本侧恢复路径。

A 无效则不跑 B；有效 native 加 host/trace warning 可以保留两侧数据。左右输出/log/CSV/trace
使用不同目录（A/B 不采用单次的 `--output-dir` / `--trace` 覆盖），B 开始前再次验证输入。
比较方向是 `delta=right-current - left-target`；负耗时/内存表示降低，左为零时 percent=null。
缺失行 N/A，配置、device/input、计时来源、内存单位/列、采样周期不一致的指标标
not_comparable。两侧 `calls_per_iteration` 或调用名称/顺序构成不同，也拒绝相关 per-call
统计收益（delta=null），但不抹掉样本或影响独立可比的内存行；leave 与 outer marker 来源不同不比较。
部署 hash 各自验证，不要求两侧 hash 相等；源码身份比较 root HEAD + tracked fingerprint +
各级子模块 actual HEAD/fingerprint，不把 branch/detached 差异当作源码变化。同 HEAD dirty 或
子模块实际变化且 B 未观察到 build、部署字节相同时，`no_detected_unbuilt_reuse=false` 并拒绝 delta。
身份/hash/build 元数据不足时该检查为 null，`comparison.build_provenance.unbuilt_reuse=unverified`，
不声称构建正确。即使 build/hash 有证据，`comparison.build_provenance.build_correctness` 仍是
`unverified`：共享 build/cache 不是 hermetic clean build，不做因果或显著性结论；输出数值
正确性始终 `not_verified`。repeat_full 是同进程 N 个生命周期，不是 N 个独立实验。

内存比较还要求实际采样质量一致：`comparison.sampling_quality` 检查实际 interval 的
median/max，以及 `duration_ms/(sample_count-1)` 的左右最大/最小比值。默认容差为 2，
可用 `--sampling-quality-tolerance 1.5` 调整（必须有限且 >=1）。点数、跨度、实际统计须
完整且一致（含统计值必须能由该点数产生，例如两点只有一个 interval，min/median/mean/max 必须相同）；
跨度缩短且点数同步下降、密度相同，不会仅因总点数变少而拒绝。
质量缺失/失配、覆盖状态不完整或不同，则内存行标 not_comparable，delta/percent=null，
保留数值/原因，不影响独立可比的性能行。共同 `comparison.comparable` 不代表每个指标可比，
应结合各行 status 和 sampling_quality。容差不能绕过缺失或非法元数据；两侧覆盖均为
unverified 时仍明确警告，质量检查通过也不证明充分采样或连续时间峰值。

### 本地回归与依赖

新增实现只用 Python 标准库，支持 Python 3.8+/POSIX（fcntl/flock、进程组、pthread_sigmask）。
启动检查 `git`、`bash`、`adb`，并要求可用 adb server/唯一设备；pipeline 原 run 脚本仍负责
NDK/CMake/编译器与系统命令，缺失时保留私有失败日志，不自动安装。配套性能表格依赖已声明的
`tabulate`；无新增第三方依赖。

```bash
python yuutils/utils/run_siq_benchmark.py --self-test
python -m unittest discover -s yuutils -p 'test_run_siq_benchmark.py'
python -m unittest discover -s yuutils -p 'test_yuutils_release.py'
python -m py_compile yuutils/utils/run_siq_benchmark.py
```

回归仅用合成日志/CSV、临时独立 Git 和本地子模块，包含非递归嵌套初始化状态、目标 gitlink/actual
不匹配、父先子后恢复、tracked/conflict 与 untracked repository 边界、stash 保留理由、中断与遗留事务；
不会运行真实设备。
`test_run_siq_benchmark.py` 进入 sdist。隔离 wheel 验证可将 `BENCHMARK_TEST_SCRIPT` 指向
临时环境的 `bin/run_siq_benchmark.py`，执行同一测试文件；不要覆盖全局安装。

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

## 会话恢复监控（1.5.3）

1.5.1 默认是前台新 PTY，**不是监控已有会话的后台服务**。1.5.2 默认改为当前 UID
后台 daemon：打印 PID 和监控范围后返回 shell，不创建对话、不打开其他 TTY。
1.5.3 修复 429 自动继续、402 Key 配额禁止和启动诊断，保留已有安全检查。
已有未接入的会话需要按下面步骤加载桥接；未验证的版本和后端会显示原因，不会假报可自动回复。

```bash
python3 -m pip install -U 'yuutils==1.5.3' --extra-index-url http://10.168.248.83:8080/simple/ --trusted-host 10.168.248.83 && step-watch start
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

- 原生 `agent_end` 的最后 assistant 必须是明确的服务器过载或暂时限流错误；`agent_settled` 后
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
- **429 自动恢复**：识别 `rate_limit_exceeded` 和换行的 `OpenAI API error (429)` /
  `Rate limit exceeded`；内置 retry / autopilot 耗尽且原生回合 settled 后，按上述安全规则
  自动发送 `continue`。不再用 wrapper CLI 的 `usage` 余额作为恢复门槛，也不访问凭据。
  每个新失败回合均可恢复，包括相同的 429，但距离上次提交至少 60 秒；同一候选不重复发送。
- **402 Key 配额耗尽禁止恢复**：同一错误中 HTTP 402 且包含
  `API Key quota has been exhausted` 或 `api_key_quota_exhausted`，返回 `quota-exhausted`；
  即使混有过载/限流文本，也绝不发送 `continue`。兼容换行与错误示例中的非标准 JSON。
  其他未知错误没有恢复候选；这不是把所有 402 都视为 Key 配额耗尽。
  此限制控制 watcher 的自动继续，不修改 StepCode 内部已开始的 provider / autopilot 重试。
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
或凭据。日志记录固定策略、首次扫描及后续状态变化，进程身份同时检查 PID/start-time、
四个 UID 和 peer credentials。

`start` 首次扫描尚未完成时明确显示 `scan=discovering`，不将其假报为没有会话。
扫描完成后 `status` / `log` 显示 `connected` / `unbridged` 数量和 `PID/state/recovery`。
日志中的 `quota-exhausted` 是 Key 配额禁止，`cooldown` 是等待 60 秒，`submitted-unconfirmed`
表示已调用恢复 API；`needs-extension-load` 提示加载桥接，`ui-safety-unavailable` 表示当前
原生版本或编辑器未通过验证。仅记录状态变化，避免每秒重复刷屏；退出进程也会记录。
启动失败会给出 `extension-conflict`、`private-directory`、`readiness-timeout` 等固定原因和
处理提示，而不会回显任意异常。旧扩展内容冲突仍不自动覆盖：先人工审阅旧文件，确认替换后
再安装 1.5.3，运行 `step-watch start` 并在已有会话中 `/reload`；新会话自动加载新桥接。

### 显式旧模式

仅需新前台 PTY 时：`step-watch run --cli stepgo -- --model <已配置模型>`。
StepGo 入口由已验证 CLI help 识别为 `agent run stepcli`；未知 CLI 拒绝启动。
该模式不是后台已有会话服务，依赖 `pyte`，SSH 断开会退出。启动显示 UID、恢复策略、
静默与冷却参数；429 包含普通错误和 retry 耗尽的换行格式，不查询余额。PTY 记录真实软折行
边界，支持 60/80/100 列的关键词折行；空输入区的局部清行不会丢掉未改动错误的折行信息。
402 Key 配额耗尽
只输出固定禁止原因，不发送继续；编辑器/输入保护和稳定非失败窗口再武装仍保持保守。
旧 Herdr 监控用 `stepcode-claude-watch.py herdr start/status/log/stop`（运行需要 PATH 中的
`herdr`、所选 CLI；跟踪日志需要 `tail`），缺少所有权证明仍不发按键。升级前先停止旧进程。

**破坏性变更：**与早期 Herdr watcher 相比，旧模式不再自动输入或恢复 Claude/StepCode，
因为当前 Herdr 无可靠目标 UID 证据。这不是“保持旧自动恢复行为不变”。需要自动恢复时，
使用上面的原生后台桥接，并遵守已验证版本、接入和错误类型限制。

实时查看旧模式日志：`stepcode-claude-watch.py herdr log --follow`。
跟踪安全验证并打开的文件描述符；日志路径被替换后仍跟踪原文件，不自动打开替换文件。

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
触发原生发送 API，在 native `input` 事件返回 `handled` **阻止模型调用**；
分别验证文字、原生图片占位、未结束粘贴、模态、overlay、402 禁止，以及两次相同 429
在真实 60 秒冷却后的恢复。TUI 测试含两段真实冷却等待，通常需约 2–3 分钟，不保存终端输出。

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
