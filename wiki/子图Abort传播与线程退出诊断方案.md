# 子图 Abort 传播与线程退出诊断方案

> 状态：阶段一诊断能力已实现。第 1–12 节说明设计约束，第 13 节说明已实现的功能、用法和实测记录。
> 目标：跟踪线程启动和 abort 传播，确认哪些子图已处理取消、哪些执行已退出，以及未退出的原因。
> 范围：诊断方案不依赖调试器栈帧，先用于隔离复现与单实例灰度；不改变取消、调度和 usage 合并策略。第 8.1 节另提供手动断点排查方法。

## 1. 方案选择与阅读指引

诊断以生命周期事件为主，线程栈为辅：

1. **生命周期事件**：在线程启动、命令入队和处理、子图终态、清理及线程退出的实际发生位置记录事件，再用关联标识还原过程。
2. **全线程栈**：出现超时或某个阶段缺少记录时，按需抓栈，定位锁等待、`Future` 等待或外部调用阻塞。通过信号触发前，必须确认目标进程已成功注册该信号。

仅靠 `sitecustomize.py` 遍历局部变量，无法完整判断所有子图是否收到 abort。事后快照无法恢复已经消失的栈帧、被回收的子图或历史命令；一个布尔状态也不能证明线程已经停止。

如果只需要抓栈，可以在启动时独立注入注册逻辑。要可靠地记录生命周期，则需要少量源码观测点或正式诊断接口。不建议为避免修改源码而使用 monkey patch、全局 trace 或反射遍历：这些方式更依赖内部结构，也更容易改变运行时行为。

按排查需要阅读：

- 先区分取消、清理和线程停止：看第 2–3 节。
- 了解事件、日志和抓栈约束：看第 4–7 节。
- 执行验证或设置断点：看第 8 节及第 8.1 节。
- 判断结果、安排验收和回滚：看第 9–12 节。
- 启用已有诊断功能、查看实测日志：直接看第 13 节。

完整的 request/cause 关联与多进程报告尚未实现，不应把设计要求视为已有能力。

## 2. 必须分开的状态

| 阶段 | 实际含义 | 不能据此推出 |
|---|---|---|
| 图开始事件 | 调用方收到 `GraphRunStartedEvent` | 工作线程已经进入执行循环 |
| 线程启动请求 | 调用了 `Thread.start()` | 线程目标函数已经运行 |
| 线程进入 | 线程目标函数开始执行 | 某个节点已开始执行 |
| abort 入队成功 | 目标命令通道的发送调用成功返回 | 目标 dispatcher 已取出或处理命令 |
| abort 已处理 | 目标 handler 已设置该图的 aborted 状态 | 阻塞的节点调用已返回 |
| 子图终态已发出 | 产生或向调用方交付 `GraphRunAbortedEvent` | `run()` 的 finally 和线程 join 已完成 |
| run 清理完成 | `run()` 的 finally 完成 | 所有有限 join 的线程均已停止 |
| Future 已完成 | iteration callable 已返回或抛异常 | executor 线程或子图 worker 已退出 |
| 线程退出标记 | 线程目标函数已到达 finally 尾部 | OS 线程在标记写入的瞬间已经死亡 |
| 线程确认停止 | join 返回后该 Thread 的 `is_alive()` 为 False | 外部服务请求已被供应商取消 |

报告需要分别回答三个问题：取消请求是否已处理、图执行到了哪个阶段、相关线程是否仍存活。不要用一个 `success/failed` 字段概括这三种状态。

## 3. 当前代码边界

以下行为经当前源码只读核查。实施时应重新确认位置，后续行号可能变化。

### 3.1 命令传播

`GraphEngine.request_abort()` 只是发送命令，没有处理确认：

```225:229:src/graphon/graph_engine/graph_engine.py
    def request_abort(self, reason: str | None = None) -> None:
        ...
```

handler 先设置当前图 abort 状态，再通过注入回调传播给子图：

```25:34:src/graphon/graph_engine/command_processing/command_handlers.py
    def handle(
        ...
```

runtime 保存子图弱引用及 abort 原因；对当前子图扇出，对晚登记子图补发。弱引用集合不是历史台账，也不是存活线程清单：

```506:539:src/graphon/runtime/graph_runtime_state.py
    def abort_children(self, reason: str) -> None:
        ...
    def create_child_engine(
        ...
```

### 3.2 Worker 与 dispatcher

Worker 在循环入口以及取得节点事件后检查 abort。若正在等待节点迭代器的下一次产出，检查点不会抢占阻塞调用；取得事件后若发现 abort，该事件可能不再转发：

```82:106:src/graphon/graph_engine/worker.py
    def _is_aborted(self) -> bool:
        ...
    def _consume_node_events(
        ...
```

```126:160:src/graphon/graph_engine/worker.py
    def run(self) -> None:
        ...
```

dispatcher 处理命令后观察停止状态；队列等待时间不等于端到端取消延迟上限。abort 后排空当前事件队列，不等于等待未来所有 worker 事件：

```84:147:src/graphon/graph_engine/orchestration/dispatcher.py
    def _dispatcher_loop(self) -> None:
        ...
```

### 3.3 清理与线程停止

dispatcher 的 stop 使用有限 join；worker pool 逐个有限 join，之后清空列表。列表为空不能证明线程已退出：

```78:82:src/graphon/graph_engine/orchestration/dispatcher.py
    def stop(self) -> None:
        ...
```

```114:132:src/graphon/graph_engine/worker_management/worker_pool.py
    def stop(self) -> None:
        ...
```

`run()` 先交付终态，再在 finally 停止子系统。消费方若停在终态 yield，不能假定清理已发生：

```246:291:src/graphon/graph_engine/graph_engine.py
    def run(self) -> Generator[GraphEngineEvent, None, None]:
        ...
```

### 3.4 iteration 与 usage

兄弟取消不能中断已经运行的 Future。旧实现的 drain 对未取消 Future 调用无超时的 `result()`，executor 上下文退出也可能继续等待。当前修复已把主 iteration 等待与 abort 后 sibling drain 都改成有界 `wait()`，executor 退出使用 `shutdown(wait=False, cancel_futures=True)`：

```433:490:src/graphon/nodes/iteration/iteration_node.py
    def _abort_parallel_siblings(
        ...
    def _drain_parallel_siblings(
        ...
```

保留 drain 能合并 child runtime 已累计的 usage，但不能补回未进入 runtime 的结果事件。超时按语义 A 处理：父 iteration 直接失败，未完成子任务留在后台，pending index 不写 `sibling_wait_end`，也不合并 usage。

> 上述代码引用使用省略形式定位方法，不是可直接应用的补丁。

## 4. 关联模型：每个子图必须有独立身份

不要只用 workflow_id、节点 ID 或线程名关联。不同子图可能共享 workflow_id，相同节点可多次执行，线程名也可能重复。

建议字段：

| 字段 | 含义 |
|---|---|
| `session_id` | 本次诊断会话，区分重启前后记录 |
| `run_id` | 本次顶层执行，优先复用宿主已有执行 ID |
| `pid` | 实际发出事件的进程 ID |
| `engine_id` | 本次会话中唯一的引擎实例 ID |
| `parent_engine_id` | 直接父图；根图为空 |
| `container_execution_id` | iteration/loop 本次执行实例，不只是静态节点 ID |
| `iteration_index` | iteration 项下标；不适用时为空 |
| `thread_instance_id` | 本次会话的线程实例标识，避免线程 ident 被重用 |
| `thread_ident / native_id / thread_name` | 辅助对应 Python 栈、系统线程和人工阅读 |
| `abort_request_id` | 一次 abort 请求的关联标识 |
| `cause_request_id` | 子图请求对应的父请求，区分嵌套传播与独立取消 |
| `timestamp / monotonic_ns` | 人工时间与进程内耗时计算 |
| `event_type / sequence` | 事件类型与顺序辅助信息 |

### 4.1 请求标识的实施边界

上述 request ID 是设计要求，不能假定当前命令已经支持。实现时需要明确采用哪种关联方式，并记录其限制：

- 若已有 trace metadata，优先复用；否则评审是否增加可选诊断 metadata，并验证内存/Redis 序列化兼容性。
- 首期也可限制为“单 run、单次外部 abort”，只做 engine 级确认，明确不能准确区分并发重复 abort 请求。
- 不用 reason 字符串相等作为请求身份，不依赖任意对象 `repr()`，不在诊断表中长期持有 child/Future/frame 强引用。

首期推荐单次外部 abort；重复、嵌套传播需要精确请求关联后再验收。

## 5. 需要记录的事件与位置

本节列出设计要求中的诊断事件，它们与业务事件分开记录。阶段一已实现的事件见第 13.1 节；本节清单不代表全部已实现。

### 5.1 创建与线程启动

| 事件 | 观测位置 | 必要内容 |
|---|---|---|
| `engine_created` | 引擎初始化成功后 | engine、parent、run |
| `child_registered` | 子图登记完成后 | parent、child、容器执行、index、是否存在待补发 abort |
| `thread_start_requested` | worker/dispatcher 启动调用前 | thread 实例、角色、engine |
| `thread_enter` | 线程目标函数入口 | thread 实例、ident/native_id |
| `iteration_task_enter` | iteration callable 入口 | 容器执行、index、executor 线程身份 |
| `iteration_child_bound` | iteration 创建并登记 child 后 | index 与 child engine 的映射 |

child runtime 登记时不一定持有 iteration index，因此要通过 `iteration_child_bound` 补齐映射，而不是猜测父栈局部变量。

### 5.2 发送与接收 abort

| 事件 | 观测位置 | 必要内容 |
|---|---|---|
| `abort_send_attempt` | 调用命令通道前 | 请求、源/目标 engine、来源类型 |
| `abort_enqueue_ok` | send 正常返回后 | 请求、目标 engine |
| `abort_enqueue_failed` | send 抛错分支 | 请求、异常类型；不记录原始异常内容 |
| `abort_handler_enter` | 目标 handler 开始处理 | 请求、目标 engine、实际处理线程 |
| `abort_state_set` | `execution.abort()` 成功后 | engine、aborted 布尔值 |
| `child_abort_targeted` | 父图扇出目标确定后 | 父请求、child、传播类型 |
| `late_child_abort_targeted` | 晚登记补发分支 | child、关联父取消状态 |
| `abort_handler_done` | handler 正常完成后 | engine、请求 |
| `abort_handler_failed` | handler 异常退出时 | engine、请求、异常类型 |

“子图收到并处理”的最低证据是其自身 `abort_state_set`。父图的 `child_abort_targeted` 或子图的 `abort_enqueue_ok` 都不能替代。

如果已设置父图状态但传播回调报错，必须显示“父图已取消，子图传播不完整”，不能因为 parent aborted=True 就认为全链路成功。

外部命令若绕过 `request_abort()` 直接进入 Redis/其他通道，需要在实际发送端与 handler 两端取证；缺少发送端记录时明确标注，不补造事件。

### 5.3 协作退出与清理

| 事件 | 观测位置 | 必要内容 |
|---|---|---|
| `worker_abort_observed` | worker abort 检查命中 | engine、worker、当前节点、检查点类型 |
| `dispatcher_abort_observed` | dispatcher 停止判断命中 | engine、thread |
| `terminal_emitted` | 图终态交付位置 | engine、终态类型 |
| `run_cleanup_enter` | run finally 入口 | engine |
| `thread_stop_requested` | 向线程发停止请求 | thread 实例 |
| `thread_target_exit` | 线程目标函数 finally 尾部 | thread 实例、退出原因 |
| `thread_join_result` | 现有 join 返回之后 | thread 实例、alive_after、等待耗时 |
| `run_cleanup_done` | run finally 正常完成 | engine |
| `iteration_task_exit` | iteration callable finally | index、child、结果类别 |

`thread_target_exit` 是“即将返回”的证据。严格确认停止以 join 后 `is_alive()==False` 或随后受控存活检查为准；如果没做最终确认，报告“退出标记已记录，停止待确认”。

必须覆盖动态扩容和缩容路径，不只覆盖引擎最终 stop。不要为了观测改动既有 join timeout，也不要额外无界 join。

### 5.4 Future、drain 与 usage

记录 `future_cancel_attempt/result`、`sibling_drain_begin`、`sibling_wait_begin/end`、`usage_merge_done`、`sibling_drain_end`，以及超时修复新增的 `parallel_wait_timeout`、`sibling_wait_timeout`、`sibling_drain_timeout`、`parallel_pending_abort`。

- cancel 结果记录真实返回值，不将返回 False 写成“取消失败导致异常”；它可能只是任务已经运行或完成。
- 每次 wait 记录 index 和 child engine，才能定位卡在哪个兄弟。
- 超时事件必须记录 `pending_indexes`、`timeout_seconds`、`elapsed_seconds`；未完成 index 不写 `sibling_wait_end`。
- usage 仅记录已经存在的计数快照与合并 index，标注为“截至当前阶段已入账”；pending index 的 usage 不合并。
- 不在信号处理器内调用 `Future.done()` 或 `cancelled()`；它们可能获取内部锁。
- 不在日志写入过程中额外遍历可变的共享对象图；优先记录业务代码在当前位置已经持有的简单值。
- parent usage 可能已包含 child usage，禁止把两者再次求和当实际费用。

## 6. 日志输出与业务隔离

建议使用独立诊断输出通道，不复用普通 `logging.Handler` 锁，也不修改当前 logging 架构。

### 6.1 推荐最小实现

- 默认关闭，显式开启后只观测目标 run 或隔离实例。
- 事件在业务路径只组装有限、白名单的简单字段，通过有界队列交给专用写入线程。
- 入队采用不等待容量的方式；队列满则丢弃诊断事件并累计 drop 计数，绝不阻塞业务等待落盘。
- “非阻塞入队”仅表示不等待队列空位，不代表实现完全无锁、完全零开销。
- 格式化与磁盘 I/O 由诊断线程完成，使用每 PID 独立 JSONL 文件；线程栈另存文件。
- 写入失败、队列溢出、异常退出都要反映为 `trace_incomplete`。缺日志时不得给出确定的“未收到”结论。
- 限制总容量、单条大小和写入频率；文件权限仅允许必要用户读取，默认使用本地磁盘。
- 退出时只做有限时间 flush；未落盘内容记为不完整，不通过无限等待阻止退出。

尽量在业务锁外提交事件。如果观测点必须位于锁内，就只在锁内提取必要的简单值，释放锁后再提交。

不同线程的事件可能交错写入。分析时应根据关联键和各阶段的因果关系还原过程，不能直接把日志文件的行序当成全局执行顺序。

### 6.2 隐私与对象生命周期

默认不记录 prompt、变量池、HTTP 内容、密钥、完整 error/reason、任意对象字符串。异常只记录类型及固定错误分类。

不得为了保留诊断历史长期保存 engine、Future 或 frame 对象。历史台账存放简单 ID 与状态事件，避免改变 WeakSet 清理时机。

## 7. 按需全线程栈

### 7.1 推荐使用方式

基础层使用 `faulthandler.register()` 直接注册经过检查的诊断信号，不通过普通 Python 信号回调先执行复杂采集。

- 启动时确认平台支持、信号未被服务框架占用。
- 打开专用文件并保持有效；日志轮转不能悄悄关闭或复用该文件描述符。
- 记录 `dump_registered`：PID、信号名称、文件路径、注册时间。
- 若框架在之后重新注册信号，必须重新核实；早期启动标记不是永久保证。
- 只有确认目标 PID 注册成功后，才允许向它发送约定信号。
- 多进程服务要区分 master、请求进程和实际执行工作流的 worker。
- 不盲目串联旧处理器；发现占用则禁用该诊断信号或选择经确认的替代方案。

**Linux 下，未注册处理器的进程收到 SIGUSR1 默认可能直接终止。不得把向任意 PID 发送 USR1 当作探测注册是否成功的方法。**

### 7.2 与生命周期记录配合

当某个 child 已 `abort_state_set`，但超过观测窗口仍没有线程停止确认时，抓取至少两次间隔快照，结合 thread ident、engine 映射与等待事件判断位置：

- 停在节点网络或 native 调用：可能尚未到协作检查点；不能因此认定命令没送达。
- 停在 `future.result()`：查看对应 `sibling_wait_begin` 的 index，再定位该 child。
- 停在 `join()`：查看此前 stop 请求及线程存活结果。
- 停在日志/其他锁：记录锁等待现象，不能仅凭一张栈就断言死锁因果。
- 某线程未出现在快照：也可能因为转储失败、覆盖限制或抓取时机，不直接认定其已退出。

纯栈不能证明供应商请求取消，也不能完整展示 native 调用内部状态。

### 7.3 sitecustomize 的适用边界

可作为纯栈注册的复现环境入口，但不是必须方案。使用前检查已有 `sitecustomize`、PYTHONPATH、是否禁用 site、fork/spawn 继承以及框架是否覆盖信号。

不在 sitecustomize 中自动 monkey patch 全部引擎方法，不在信号回调内遍历 locals、调用 Future 方法或写复杂日志。其 `--self-check` 若实现，应作为显式脚本入口设计，不能在自动导入时解析或消费业务进程参数。

## 8. 实际验证流程

### 步骤一：准备隔离场景

构造一个可重复的父图，包含并行 iteration；明确每个 index 的行为：

- A：持续产生事件、能很快到达检查点。
- B：停在可控阻塞调用，测试收到 abort 但尚未退出。
- C：通过同步屏障推迟创建，测试晚登记补发。
- D：在 abort 前已正常完成，测试历史子图不会被误报成取消失败。

离线测试使用 Event/Barrier 和可释放的替身节点，不调用真实 LLM、HTTP、Redis；Redis 集成另设受控环境。测试结束必须释放 B/C，避免测试自身无限等待。

### 步骤二：启动并核实观测链

1. 建立 session/run 标识，确认生命周期输出和栈输出都可用。
2. 确认父图与已启动 child 的 `engine_created`、`child_registered`、`iteration_child_bound`。
3. 确认对应 `thread_enter`，不要仅等待 `GraphRunStartedEvent`。
4. 记录目标工作流进程 PID；若使用信号，核实该 PID 的注册状态。

### 步骤三：发送真实 abort

1. 从此次要验证的真实入口发送：宿主 stop 接口/命令通道或受控的 `request_abort()`。
2. 记录发送请求及 `abort_enqueue_ok`，确认父 handler 的 `abort_state_set`。
3. 按子图检查扇出目标、入队、子图 handler 状态。
4. 释放 C 的创建屏障，检查晚登记补发是否发生及是否被 child handler 处理。
5. 不通过直接手改 aborted 布尔值替代命令链路测试。

诊断信号只负责抓栈，不负责发送业务 abort；两者必须分开操作和标记。

### 步骤四：观察退出

逐个收集子图的退出证据：worker 是否命中检查点、是否发出终态、`run()` 是否完成清理、线程 join 后是否仍存活，以及 iteration 任务返回情况和 drain 进度。

观测窗口应可配置，例如首次观察 5 秒、第二次观察 30 秒。这个时间只用于触发进一步排查，不代表产品取消 SLA，也不会自动取消执行。超过窗口后仍继续观测，不强杀线程、不修改 `Future` 状态。它与有界等待修复中的 `GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT` / `GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT` 是两个不同概念：前者是人工排查窗口，后者是产品执行策略。

场景 B 预期会先出现“abort 已处理，但节点仍阻塞”的状态，此时 worker 可能仍存活。先抓栈，再释放替身节点的阻塞，最后确认线程退出，以此验证“已收到但未退出”的分类是否准确。

### 步骤五：检查新一轮隔离

- 同一父图正常推进到另一个 iteration 时，检查其新容器执行 ID 和新 child 映射；上一 iteration 的局部 sibling 取消不应被误当成新 child 的取消。
- 真正的父图 abort 后，已被该父执行覆盖的晚登记 child 收到补发是预期行为，不是跨轮污染。
- 新建顶层 run 使用新 run/engine 标识，确认未继承旧执行的取消状态。
- 不假定“下一个 iteration”一定新建父 runtime；必须依据实际父子 engine 关系判定隔离。

### 步骤六：生成诊断报告

输出每个 child 的时间线和汇总表，附日志完整性及栈抓取时间。无法确证的状态明确写 unknown，不写成失败事实。

## 8.1 现有代码的断点与调试控制台操作

即使没有生命周期诊断模块，也可以用本节的断点和控制台表达式排查。仅用于隔离复现或已批准的调试实例，不建议暂停承载真实流量的共享进程。

本节路径均相对 `src/graphon/`。行号对应编写时核查的源码版本；代码变化后，应按方法名和语句重新定位。

### 8.1.1 操作前须知

- 调试控制台表达式使用**当前选中的线程和栈帧**。下表中不同位置的 `self` 分别是 engine、handler、bindings、pool、worker，不能混用。
- 断点通常在该行执行前停住。例如停在 `execution.abort(reason)` 所在行，状态尚未被这行修改；要在下一行检查结果。
- 先记录对象 ID、线程标识及 iteration index，再用条件断点过滤；父子图可能共享 `workflow_id`，不能靠它唯一识别 child。
- 记录 `id(...)` 时保留十进制整数，便于复制到条件表达式。对象 ID 只在本进程、该对象存活期间有效，不是持久的 run ID。
- 发送 abort 后必须继续运行目标线程。若调试器暂停所有线程，dispatcher 无法消费命令，此时等待状态变化没有意义。
- 高频检查使用条件断点或简单 logpoint；不要在持有业务锁的位置长时间停留。
- 控制台命令本身也可能执行代码、获取锁；出现求值超时立即停止继续求值，不要重复提交。

### 8.1.2 断点清单：按一次验证的顺序设置

| 顺序 | 文件与位置 | 停止时机 / 目的 | 当前帧可用对象 |
|---|---|---|---|
| 1 | `graph_engine.py`，`GraphEngine.run()`，调用 `_start_execution()` 之后 | 记录父 engine 及 graph execution 身份；不是在 started 事件 yield 处 | `self` 为 engine |
| 2 | `worker_management/worker_pool.py`，第 150 行 | `worker.start()` 已返回；记录线程身份，但还需目标函数入口确认 | `worker`、`self` 为 pool |
| 3 | `worker.py`，`Worker.run()` 首个可执行语句 | 确认 worker 实际进入 | `self` 为 Worker |
| 4 | `orchestration/dispatcher.py`，第 87 行 | 确认 dispatcher 已进入主循环 | `self` 为 Dispatcher |
| 5 | `nodes/iteration/iteration_node.py`，第 508 行 | 已完成 index→child 登记并释放登记锁，记录对应关系 | `index`、`graph_engine` |
| 6 | `graph_engine.py`，第 227 行 | 确认向哪个 engine 发 abort；此时尚未完成发送 | `self` 为目标 engine |
| 7 | `command_processing/command_handlers.py`，第 33 行 | `execution.abort(reason)` 已执行，分别观察父图与子图 | `execution`、`command`、`self` 为 handler |
| 8 | `runtime/graph_runtime_state.py`，第 511 行 | 已记录 abort 原因并取得当前 child 快照，锁已释放 | `children`、`self` 为 bindings |
| 9 | 同文件第 514 行 | 当前 child 即将收到发送请求；不是已处理确认 | `child` |
| 10 | 同文件第 538 行 | 命中晚登记补发分支；此处仍持登记锁，只短暂停留或用 logpoint | `child_engine` |
| 11 | `worker.py`，`_consume_node_events()` 的 abort 分支内部 | 确认 worker 实际观察到 abort，勿只断在每次条件检查前 | `self` 为 Worker |
| 12 | `orchestration/dispatcher.py`，第 94 行 | 停止分支命中；还需区分 aborted 与 execution_complete | `self._execution_coordinator` |
| 13 | `graph_engine.py`，`_emit_terminal_events()` 的 aborted 分支 | 确认图交付 abort 终态；尚不能证明清理完成 | `self` 为 engine |
| 14 | `worker_management/worker_pool.py`，第 132 行 | 所有既有 join 已返回，列表尚未清空，检查仍存活 worker | `self._workers` |
| 15 | `nodes/iteration/iteration_node.py`，第 481 行 | 即将等待某个 sibling Future，记录被等待的 index | `index`、`future`、`started_child_engines` |
| 16 | 同文件第 483 行 | 该次 `result()` 已返回或异常被抑制，不代表调用成功 | 同上 |

最小组合先设置第 5、7、8、13、14、15 项；遇到发送或启动问题再启用其他断点。若要确认 dispatcher join 后状态，在 `Dispatcher.stop()` 的调用方、该调用返回后的下一条语句检查对应 thread；不要把第 82 行 join 执行前当作返回后。

### 8.1.3 第一步：记录父 engine 与 child 映射

选中 **父 `GraphEngine.run()` 帧**，在控制台逐条执行以下表达式（只读）：

- `id(self)`：父 engine ID。
- `id(self._graph_execution)`：父 execution ID。
- `(self._graph_execution.aborted, self._graph_execution.completed)`：当前状态。

选中 **`_execute_tracked_iteration_parallel()` 第 508 行帧**：

- `(index, id(graph_engine), id(graph_engine._graph_execution))`
- `(graph_engine._graph_execution.aborted, graph_engine._graph_execution.completed)`

把每个 index 对应的 engine/execution ID 记到外部笔记。不要把多个 engine 保存进调试器全局变量长期持有，否则会改变弱引用注册表的回收时机。

在 **`AbortCommandHandler.handle()` 第 33 行**设置条件断点：`id(execution) == 123456789`。将数字替换为刚记录的目标 execution ID。父图和子图分别核实；不能在这里使用 `self._graph_execution`，因为 `self` 是 handler。

### 8.1.4 第二步：从控制台发送 abort

这是本节唯一主动改变执行状态的操作，只对你明确要终止的测试执行调用一次。

**方式 A：验证父图向全部子图传播。** 切回父 `GraphEngine.run()` 帧，核对 `id(self)` 后执行：

`self.request_abort("debug-parent-abort")`

这里 `self` 必须是 `GraphEngine`。别在 `GraphWorker-N.run` 帧执行这句；那个帧里的 `self` 是 `Worker`，没有 `request_abort()`，IDE 会报 `AttributeError: 'Worker' object has no attribute 'request_abort'`。

**方式 B：只对某个 child 发请求。** 在 `_execute_tracked_iteration_parallel()` 第 508 行帧，核对 `index` 和 `id(graph_engine)` 后执行：

`graph_engine.request_abort("debug-child-abort")`

这里要在 iteration 子帧执行。若选到 `ThreadPoolExecutor-1_x` 的通用 `_worker` / `run` 帧，局部变量 `graph_engine` 不存在，会报 `NameError: name 'graph_engine' is not defined`。

方式 B 不等价于验证外层取消；该 child 的 abort 可能触发 iteration 兄弟互撤。因此它也不保证只影响一个 iteration 项。

随后立即使用 IDE 的 Continue/继续执行（通常为 F5，依 IDE 键位而定），让 dispatcher 消费命令。控制台显示 `None` 只代表调用正常返回，不是取消完成确认。

此操作验证 engine 命令通道及后续链路，**不验证宿主 HTTP stop 接口、权限或 Redis 路由是否正确**。要验证那些入口，应从真实调用方发 stop，再在 handler 断点观察；本文不猜测宿主接口地址和请求体。

禁止替代操作：直接赋值 `aborted=True`、调用 `execution.abort()` 或 `abort_children()` 来冒充完整命令传播；禁止通过 `future.cancel()`、清空 ready queue、再次调用 `run()` 人工制造通过结果。只想看 abort 后的收尾行为时，可以用第 13.4 节的直接置位方法，但那不算命令传播验证。

### 8.1.5 第三步：检查哪些子图被发送、哪些已处理

在 **bindings 的 `abort_children()` 第 511 行**，`children` 已是锁内生成的 tuple，可读取：

- `[(id(c), id(c._graph_execution), c._graph_execution.aborted, c._graph_execution.completed) for c in children]`
- `self._child_abort_reason is not None`

此时扇出循环尚未执行，因此 children 中 aborted=False 完全可能是正常现象。此列表不包含未来才登记的 child，也不能被当作全历史子图列表。

在 **第 514 行**：

- `(id(child), id(child._graph_execution))`

在 **晚登记分支第 538 行**：

- `(id(child_engine), id(child_engine._graph_execution), self._child_abort_reason is not None)`

在 **目标 child 的 handler 第 33 行**：

- `(id(execution), execution.aborted, execution.completed)`
- `self._on_abort is not None`

第 33 行的 `execution.aborted == True` 是该 child 已处理请求的直接证据；接下来的回调是否成功、嵌套 child 是否处理，还要继续观察。不要输出完整 command/reason/error，避免敏感内容泄漏。

### 8.1.6 第四步：确认工作线程是否真的停止

在 **`Worker.run()` 帧**：

- `(id(self), self.name, self.ident, self.native_id)`
- `(id(self._graph_execution), self._is_aborted())`

在 **`WorkerPool.stop()` 第 132 行，执行 clear 之前**：

- `[(id(w), w.name, w.ident, w.native_id, w.is_alive()) for w in self._workers]`

该位置能识别“join 已返回但线程还活着”。若存在 True，不能写取消完成。此处持有 pool 锁，应读完即继续；不要在此调用其他线程需要该锁的业务方法。

在 **持有 dispatcher 对象的栈帧**检查其 `_thread`；若当前正好是 `Dispatcher.stop()` 帧：

- `None if self._thread is None else (self._thread.name, self._thread.ident, self._thread.is_alive())`

只有在 join 返回后读取才表示停止等待的结果。若 IDE 无法在函数尾停住，回到调用方返回后的下一条语句检查；不要重新调用 `stop()`。

必要时列出进程线程，控制台先执行 `import threading`，再执行：

`[(t.name, t.ident, t.native_id) for t in threading.enumerate()]`

此操作会获取 threading 内部锁，仅作可选辅助；若其他线程被暂停在相关锁内，求值可能阻塞。重复线程名不能证明它属于哪个 child，必须结合此前记录的 ident/线程实例。活线程列表缺项也不能代替完整的退出历史。

### 8.1.7 第五步：定位 sibling drain 卡在哪个 index

在 **`_drain_parallel_siblings()` 第 481 行**：

- `index`
- `id(future)`
- `None if started_child_engines.get(index) is None else (id(started_child_engines[index]), id(started_child_engines[index]._graph_execution), started_child_engines[index]._graph_execution.aborted, started_child_engines[index]._graph_execution.completed)`
- `index in merged_usage_indexes`

继续执行，观察是否命中同一 index 的第 483 行。未命中时，结合其他线程栈判断 child 是否仍阻塞；第 483 行命中只证明等待已经结束或异常被抑制。

**不要在控制台执行 `future.result()`、`join()`、`wait()` 或获取业务锁。** 这些操作可能使调试器求值线程等待被暂停的线程。也不要默认调用 `future.done()`/`cancelled()`：它们会获取 Future 内部锁；尤其在暂停所有线程的场景可能卡住。优先查看已有局部值、真实执行路径及线程栈。

### 8.1.8 单次调试记录模板

每次命中记录一行：时间、PID、iteration index、engine ID、execution ID、当前线程 ident、断点阶段、aborted/completed、join 后 alive。

至少回答：

1. 父请求是否真正进入 handler？
2. 哪些 child 在扇出快照中，哪些通过晚登记补发？
3. 哪些 child 自己的 handler 第 33 行被命中？
4. 哪些 child 只有终态，哪些已完成清理且 worker 停止？
5. 未退出 child 当前卡在哪个线程/调用，drain 正在等哪个 index？

若某个断点未命中，先排除条件错误、加载源码版本不一致、断点绑定失败、目标线程仍暂停和子图早已结束，再讨论命令丢失。

### 8.1.9 与信号快照的关系

调试控制台方法不依赖诊断信号处理器，也不能证明处理器已安装。阶段一已提供生命周期记录和信号注册能力，启用方式见第 13 节；目标进程是否实际启用，仍需单独核实。

确认目标 PID 已注册诊断信号、信号未被其他处理器占用后，才可在**系统终端**发送约定信号。例如，约定使用 SIGUSR1 时，命令为 `kill -USR1 <已确认的PID>`。这条命令用于抓栈，既不是 Python 调试控制台表达式，也不是业务 abort 指令。

没有注册确认时不要发送信号，也不要在控制台用 `os.kill()` 向当前进程发信号来探测。

## 9. 报告格式与判定规则

示例为期望报告格式，不是实测数据：

| child / index | abort 入队 | 子图已处理 | 图终态 | run 清理 | worker 确认停止 | 分类 |
|---|---|---|---|---|---|---|
| child-A / 0 | 是 | 是 | aborted | 完成 | 是 | 取消已处理，执行线程已停止 |
| child-B / 1 | 是 | 是 | aborted 或待观察 | 完成或待观察 | 否 | 已处理，仍有线程存活 |
| child-C / 2 | 补发成功 | 是 | aborted | 完成 | 是 | 晚登记补发验证通过 |
| child-D / 3 | 未要求或迟到入队 | 无需处理 | succeeded，早于 abort | 完成 | 是 | abort 前已结束，不算漏取消 |
| child-E / 4 | 是 | 未观测 | 未知 | 未知 | 未知 | 入队后未观测到处理，需排查 |

### 分类规则

1. **尚未创建/提交前取消**：不存在已创建 child 的证据；根据 task/future 事件单独列出，不伪造 child 行。
2. **发送失败**：有明确 send 异常；不是单纯缺少 enqueue 日志。
3. **已入队，处理未确认**：有 enqueue，无 handler 状态证据；可能尚未消费、处理异常、诊断丢失或引擎已结束。
4. **已处理，尚未退出**：有 child abort_state_set，且其执行线程仍 alive；用栈定位等待点。
5. **清理返回，但仍有线程存活**：有 cleanup_done，join_result 仍 alive；重点暴露有限 join 的边界。
6. **执行线程已停止**：目标 engine 关联的全部 worker/dispatcher 均已确认停止，并说明 iteration callable 的独立状态。
7. **已提前结束**：终态和停止证据早于 abort，不要求已结束引擎重新启动来消费迟到命令。
8. **证据不完整**：任何队列溢出、写入失败、缺启动映射或进程异常退出，都降低判定置信度。

“哪些没有收到”应默认表述为“哪些未观测到处理”。只有链路证据完整且存在明确发送/处理失败，才给出对应确定结论。

耗时分三段统计：

- 入队 → 设置取消状态；
- 设置取消状态 → 发出图终态；
- 设置取消状态 → 确认线程停止。

不同进程的 monotonic 时钟不能直接相减。没有可靠的时钟同步时，只报告进程内耗时，并说明跨进程事件的因果关系。

## 10. 验收与故障注入

| 场景 | 验收目标 |
|---|---|
| 正常无 abort | 完整启动/退出链，诊断关闭时不创建文件或线程 |
| 父 abort、多个活跃 child | 所有目标 child 均有独立入队及处理证据 |
| 晚登记 child | 登记后补发，并由 child handler 确认 |
| 预先已结束 child | 正确归为已结束，不要求处理迟到命令 |
| child 同步阻塞 | 能区分已处理 abort 与 worker 未退出 |
| Future 尚未开始/已运行 | cancel 返回值与实际任务生命周期一致 |
| 有限 join 超时 | 清理完成不能掩盖 alive_after=True |
| 多层子图 | engine 父子关系完整，传播逐层可追踪 |
| 局部 sibling abort 后继续其他容器 | 不跨容器执行 ID 误关联 |
| 重复 abort | 单次模式明确限制；精确模式验证 request/cause 关系 |
| fail-branch/loop break | 不误判为 GraphRunAbortedEvent 或全局取消 |
| usage/drain | 不改变已有合并、去重和等待行为；不承诺供应商费用完整 |
| 队列满、慢盘、不可写 | 业务不等待诊断 I/O，报告 evidence incomplete |
| 信号占用、未注册、fork/spawn | 不覆盖既有处理器；不向未确认 PID 发信号 |
| 连续快照、线程较多 | 输出有界，记录转储失败/限制，评估暂停和时延 |

测试不仅验证“日志出现标记”，还要验证标记在真实阶段之后出现，并用屏障制造可重复的竞态。采用有界等待；测试 teardown 释放替身阻塞并确认清理。

## 11. 实施阶段与回滚

### 阶段一：隔离复现可用

- 明确单 run/单外部 abort 限制，增加最小 engine/thread 关联及生命周期事件。
- 独立纯栈注册入口，注册成功后才允许触发。
- 用可控替身验证 A/B/C/D 场景和报告分类。
- 验证正常执行结果、异常分支与 usage 合并未因诊断改动而变化。

### 阶段二：单实例灰度

- 限定目标 run；核实宿主进程、信号占用、已有 sitecustomize 与部署 Python 版本。
- 对比开启前后的吞吐、延迟、内存、诊断队列丢弃数及磁盘写入量。
- 将性能容忍阈值交由部署方确定，不在未实测前声称零影响。

### 阶段三：按需要扩展

确实需要多次并发取消或多进程精确关联时，再增加兼容的 request metadata、跨进程报告和更完整的集成验证，不把这些作为首期前置大改。

### 回滚

关闭诊断开关后，按部署流程重启并确认诊断已停用。若清除启动时注入的环境配置，应恢复原 `PYTHONPATH`，保留其他配置。

如果支持运行时卸载，必须先注销信号，再关闭对应文件，同时处理诊断写入线程。不得直接关闭仍被 `faulthandler` 使用的文件描述符。

回滚不删除项目 `.codebuddy`，不改变业务日志配置，不强行释放业务锁，不强杀运行中的节点线程。

## 12. 风险与不承诺事项

- 取消是协作式的，不提供同步调用抢占，也不保证外部服务即时停止计费。
- 诊断可能改变线程时序；即使关闭复杂 locals 采集，也不能保证零扰动。
- 最佳努力日志允许丢弃，因此必须有 evidence incomplete 分类；不能把无记录等同于未发生。
- 诊断方案本身不重构 logging、不修改 join 策略，也不通过 monkey patch 改变业务代码。
- 后续有界等待修复已改变 drain/usage 策略：超时后父 iteration 失败，pending index 不合并 usage；这与早期“只诊断、不修复”的阶段边界不同。
- 人工排查超时只用于触发抓栈；产品执行超时由 `GRAPHON_PARALLEL_*_TIMEOUT` 控制，两者不可混用。
- 完整 request/cause 关联、多进程精确报告、A/B/C/D 故障注入矩阵未实现，仍属于后续阶段。

---

## 13. 阶段一实现说明

### 13.1 已实现

- `src/graphon/diagnostics.py`：默认关闭的轻量诊断模块。
  - 事件通过有界队列交给专用写入线程，非阻塞入队；队列满时丢弃并累计 `dropped`。
  - JSONL 日志按 PID 会话写入，包含 `session_id`、`pid`、`sequence`、时间戳、线程标识与事件字段。
  - 可选 `faulthandler` 注册：发现目标信号已被占用则记录 `dump_registered_failed`，不覆盖既有处理器。
  - 退出时有限时间 flush，并注销信号、关闭文件；未落盘内容通过 `trace_incomplete` 标记。
- 观测点已加入：
  - `engine_created`、`abort_send_attempt/enqueue_ok/enqueue_failed`；
  - `abort_handler_enter/state_set/done/failed`；
  - `child_registered`、`child_abort_targeted`、`late_child_abort_targeted`；
  - `worker_abort_observed`、`dispatcher_abort_observed`；
  - `terminal_emitted`、`run_cleanup_enter/done`；
  - `thread_start_requested/enter/target_exit/join_result`；
  - `iteration_task_enter/child_bound`；
  - `future_cancel_attempt/result`、`sibling_drain_begin/wait_begin/wait_end/usage_merge_done/drain_end`；
  - 有界等待超时：`parallel_wait_timeout`、`sibling_wait_timeout`、`sibling_drain_timeout`、`parallel_pending_abort`。

### 13.2 启用方式

诊断开关由 `src/graphon/diagnostics_config.py` 的 `DiagnosticsConfig` 读取，并在 `graphon/__init__.py` 集中导出。字段使用 `Field` 描述与类型校验。并行 iteration 的两个执行超时变量由 `src/graphon/nodes/iteration/iteration_node.py` 中的 `_timeout_from_env()` 读取；非正数、非有限值或非法字符串会回落到默认值，避免把诊断配置失败变成无界等待。

```bash
export GRAPHON_TRACE_FILE=/home/jackson/graphon/debug/graphon_trace.jsonl
export GRAPHON_TRACE_SIGNAL=SIGUSR1
# 可选，默认值如下
export GRAPHON_TRACE_QUEUE_SIZE=10000
export GRAPHON_TRACE_FLUSH_TIMEOUT=0.5
```

并行 iteration 有界等待的两个变量不在 `DiagnosticsConfig` 中，默认值如下：

```bash
export GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT=300
export GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT=30
```

它们和 `GRAPHON_TRACE_*` 一样需要绑定到实际执行 workflow 的 Celery worker 进程，IDE 场景中同样要写进 `launch.json` 的 `env`。

这组 `export` 只作用于当前 shell。用 VSCode / CodeBuddy 的 F5 compound 时，Flask 和 Celery 是两个独立调试配置，环境变量不会自动互相继承。实测结果就是 Flask 拿到了 `GRAPHON_*`，Celery worker 没有。要让诊断覆盖真正执行 workflow 的进程，必须在 `launch.json` 的 `Celery Worker (Debug)` 配置里也加 `env`：

```jsonc
"env": {
  "GRAPHON_TRACE_FILE": "/home/jackson/graphon/debug/graphon_trace.worker.jsonl",
  "GRAPHON_TRACE_SIGNAL": "SIGUSR1",
  "GRAPHON_TRACE_QUEUE_SIZE": "10000",
  "GRAPHON_TRACE_FLUSH_TIMEOUT": "0.5"
}
```

也可以不用 IDE，直接在终端把变量绑到 Celery 进程上。最省事的是命令前缀：

```bash
cd /home/jackson/ai-dify/api
env GRAPHON_TRACE_FILE=/home/jackson/graphon/debug/graphon_trace.worker.jsonl \
    GRAPHON_TRACE_SIGNAL=SIGUSR1 \
    GRAPHON_TRACE_QUEUE_SIZE=10000 \
    GRAPHON_TRACE_FLUSH_TIMEOUT=0.5 \
    uv run celery -A app.celery worker -P solo <其余参数不变>
```

这些变量只传给这条命令及其子进程，不会污染当前 shell。

另一种做法是把变量写进专用 env 文件，再让 `uv run` 读取：

```bash
# /home/jackson/graphon/debug/graphon.env
GRAPHON_TRACE_FILE=/home/jackson/graphon/debug/graphon_trace.worker.jsonl
GRAPHON_TRACE_SIGNAL=SIGUSR1
GRAPHON_TRACE_QUEUE_SIZE=10000
GRAPHON_TRACE_FLUSH_TIMEOUT=0.5
```

```bash
cd /home/jackson/ai-dify/api
uv run --env-file /home/jackson/graphon/debug/graphon.env \
    celery -A app.celery worker -P solo <其余参数不变>
```

`uv run --env-file` 会把文件里的变量加载给这条命令。不用 `uv` 时，也可以在同一个 shell 里 `set -a; source /home/jackson/graphon/debug/graphon.env; set +a`，再启动 Celery。

建议 Flask 和 Celery 用不同的日志文件，例如 `graphon_trace.jsonl` 给 API，`graphon_trace.worker.jsonl` 给 worker。这样看日志时不用靠 `pid` 和 `session_id` 区分两个进程。

启动目标进程后，需要抓取全线程栈时在**系统终端**执行：

```bash
kill -USR1 <已确认注册的PID>
```

事件日志和栈日志分别写入：

- `/home/jackson/graphon/debug/graphon_trace.jsonl`
- `/home/jackson/graphon/debug/graphon_trace.stacks.txt`

不要向未确认注册的 PID 发 `SIGUSR1`；Linux 下未注册该信号的进程可能直接终止。

### 13.3 阶段一边界

- 不记录 prompt、变量池、HTTP 内容、密钥、完整 reason 或任意对象 `repr()`。
- 首期适用于单 run、单次外部 abort 的隔离复现；重复并发 abort 无法精确区分 request/cause。
- 阶段一诊断本身不修改取消、调度、drain、usage 合并策略，也不使用 monkey patch 或全局 trace。后续 13.8 的有界等待修复已按语义 A 调整 drain/usage 策略，不属于阶段一诊断能力。
- 离线自检：`tests/graph_engine/test_diagnostics.py`。
- 本地验证：`make check` 与定向 `pytest` 全绿。

### 13.4 调试记录：直接设置父图 abort 状态

本节保留直接设置 abort 状态后的实测现象。这种操作可以观察取消后的收尾行为，但绕过了命令发送和处理过程，不能代替第 8.1 节的完整传播验证。

调用前先确认当前栈帧中 `self` 的类型。以下操作发生在 `GraphWorker-1.run` 帧：

```python
self._graph_execution.abort("debug-parent-abort")
```

原因：

- 此帧里的 `self` 是 `Worker`，没有 `request_abort()`；
- `Worker._is_aborted()` 判断的是 `self._graph_execution.aborted`；
- 因此直接调用 `GraphExecution.abort()` 能立即把父图执行状态置为 aborted。

不同栈帧中的调用含义如下：

- `GraphEngine` 帧：`self._graph_execution.abort("debug-parent-abort")` 同样是直接设置执行状态，不经过命令通道。要验证正式命令通道，切到父 `GraphEngine.run()` 帧，按第 8.1.4 节执行 `self.request_abort()`。`Worker` 帧没有 `request_abort()`；那里只能直接改执行状态，见本节前面的说明。
- `ThreadPoolExecutor-N_M` 的 iteration 子帧：`graph_engine.request_abort("child debug reason")` 向当前子图发送取消请求；该请求可能触发兄弟子图收尾，不能保证只影响这个子图。
- `Worker` 帧：`self._stop_event.set()` 用于请求当前 worker 退出循环，不等价于图级 abort，也不能替代传播验证。

已观察现象：

- 在 `GraphWorker-1.run` 帧执行 `self._graph_execution.abort("debug-parent-abort")` 后，出现 `GraphRunAbortedEvent` 对应的异常路径；
- 随后 `ThreadPoolExecutor-1_N` 子图线程逐个消失；
- 最后 `GraphWorker-1` 也退出。

解释：

- 这说明父图 abort 状态已经被 worker / dispatcher 观察到，迭代子任务随 abort 路径收尾；
- “线程消失”表示任务结束、线程不再存活，**不是被强制杀死**；
- 调试器停在 `ready_queue.get(timeout=0.1)` 的 `_queue.Empty` 是正常轮询超时；若 IDE 勾选了 “Raised Exceptions”，需要在异常断点中取消 `_queue.Empty`，否则每次空队列轮询都会断下。

注意：若进程加载的是 `api/.venv/lib/python3.12/site-packages/graphon` 中的旧版本，而不是本仓库 `/home/jackson/graphon` 的新提交，则新的诊断事件不会出现在该进程日志中。

### 13.5 用执行对象 ID 关联 iteration 与父图

`GraphWorker-N` 是图引擎的 worker 线程，`ThreadPoolExecutor-1_x` 是 Python 线程池执行 iteration 子项的线程。线程名之间没有直接映射关系，需要先用父图执行对象 ID 确认它们是否属于同一次父图执行。

在 `ThreadPoolExecutor-1_x` 的 `_execute_tracked_iteration_parallel()` 帧中，`self` 是 `IterationNode`，执行：

```python
id(self.graph_runtime_state.graph_execution)
```

得到该 iteration 所在父图执行对象 id。

在每个仍存活的 `GraphWorker-N.run` 帧中执行：

```python
id(self._graph_execution)
```

执行对象 ID 一致，说明该 `GraphWorker` 与 iteration 属于同一次父图执行。多个 worker 可能共享这个 ID，因此还不能据此确定具体由哪个 worker 提交；进一步定位的方法见第 13.7 节。

注意不要拿下面这个值去和 `GraphWorker` 比较：

```python
id(graph_engine._graph_execution)
```

这是**子图执行对象**，每个 iteration 子项都有独立的 `GraphExecution`；`GraphWorker` 持有的是父图执行对象。子图 id 与父 worker id 不一致是正常现象。

如果调试器提示 `Unable to find thread for evaluation`，说明该线程已经结束、处于 stale 列表，或不是可求值上下文，并不代表表达式本身错误。先在明确活着的 `MainThread` 或 `GraphWorker-0` 帧里列出线程：

```python
import threading

[
    (t.name, t.ident, t.is_alive())
    for t in threading.enumerate()
    if "ThreadPoolExecutor" in t.name or "GraphWorker" in t.name
]
```

如果需要稳定抓到某个 `ThreadPoolExecutor-1_x` 的帧，应在 `_execute_tracked_iteration_parallel()` 内提前下断点，等断点命中后再求值，而不是恢复执行后手动选择线程。

### 13.6 识别真正的 Celery worker 子进程与日志验证

F5 启动 compound 后，`ps -ef | grep celery | grep -v grep` 出现两个进程，按下列优先级判断哪个是真正执行 workflow 的进程。

**判据优先级：**

1. `graphon_trace.worker.jsonl` 中事件记录的 `pid`；
2. `/proc/<pid>/environ` 是否存在 `GRAPHON_*` 环境变量；
3. 父子进程关系与命令行形态；
4. CPU 时间占比。

实测进程信息：

```text
(dify-api) jackson@test:~/graphon$ ps -ef | grep celery | grep -v grep
jackson  3341580 3159488  1 15:29 pts/15   00:00:00 /home/jackson/ai-dify/api/.venv/bin/python /home/jackson/.codebuddy-server-cn/extensions/ms-python.debugpy-2026.6.0-linux-x64/bundled/libs/debugpy/adapter/../../debugpy/launcher 33575 -- -m celery -A app.celery worker -P solo --without-gossip --without-mingle --loglevel INFO -Q dataset,priority_dataset,priority_pipeline,pipeline,mail,ops_trace,app_deletion,plugin,workflow_storage,conversation,dataset_summary,workflow,schedule_poller,schedule_executor,triggered_workflow_dispatcher,trigger_refresh_executor,trigger_refresh_publisher,retention,workflow_based_app_execution,api_token,celery
jackson  3341605 3341580 96 15:29 pts/15   00:00:29 /home/jackson/ai-dify/api/.venv/bin/python -X frozen_modules=off /home/jackson/.codebuddy-server-cn/extensions/ms-python.debugpy-2026.6.0-linux-x64/bundled/libs/debugpy/adapter/../../debugpy/launcher/../../debugpy --connect 127.0.0.1:42849 --configure-qt none --adapter-access-token be43d40204a39fc9215976899f4257fca73a6bec7d267fe4f1c27d2fc96db0f9 -m celery -A app.celery worker -P solo --without-gossip --without-mingle --loglevel INFO -Q dataset,priority_dataset,priority_pipeline,pipeline,mail,ops_trace,app_deletion,plugin,workflow_storage,conversation,dataset_summary,workflow,schedule_poller,schedule_executor,triggered_workflow_dispatcher,trigger_refresh_executor,trigger_refresh_publisher,retention,workflow_based_app_execution,api_token,celery
```

解释：

- `3341580` 的 PPID 是 IDE 服务进程，命令行是 `/debugpy/launcher 33575 -- -m celery ...`，CPU 时间几乎为 0，是 launcher；
- `3341605` 的 PPID 是 `3341580`，命令行带 `--connect 127.0.0.1:42849 -m celery ...`，并且 CPU 占用约 96%，是实际运行 `app.celery` 的 Python 子进程；
- `/proc/3341605/environ` 有 `GRAPHON_*`，而 `/proc/3341580/environ` 没有；
- worker 日志里记录的 `pid` 是 `3341605`。

这次实测中，执行 workflow 的是子进程 `3341605`，`3341580` 只是 launcher。确认信号注册成功后，抓栈信号应发给 `3341605`。

这些 PID 只对应本次记录。重新启动后，应按上面的判据确认新 PID，不要直接复用日志中的数值。

**核对环境变量**

检查执行进程是否收到诊断配置：

```bash
tr '\0' '\n' < /proc/3341605/environ | grep '^GRAPHON_'
```

输出：

```text
GRAPHON_TRACE_FILE=/home/jackson/graphon/debug/graphon_trace.worker.jsonl
GRAPHON_TRACE_SIGNAL=SIGUSR1
GRAPHON_TRACE_QUEUE_SIZE=10000
GRAPHON_TRACE_FLUSH_TIMEOUT=0.5
```

安装包提交验证：

```bash
cat /home/jackson/ai-dify/api/.venv/lib/python3.12/site-packages/graphon-0.4.0.dist-info/direct_url.json
```

输出：

```json
{"url":"ssh://git.szkingdom.com/grkj/websuites/graphon","vcs_info":{"vcs":"git","commit_id":"8188be26a4b9c22bfd486056592891d184f9adf7"}}
```

这次核对确认，虚拟环境中的安装包对应带诊断功能的新提交。

**本次记录停在哪个阶段**

- 日志中只有 `trace_configured` 和 `dump_registered`，说明诊断模块已启用、信号已注册。
- 当时尚未触发 workflow，因此没有 `engine_created`、`abort_*`、`iteration_*`、`thread_*` 等生命周期事件。这种情况不能判断为事件采集失败。
- 触发 workflow 后，应继续检查 worker 日志是否增长。本次进程已确认注册，可用 `kill -USR1 3341605` 抓栈；重启后须重新确认 PID 和注册状态。

### 13.7 GraphWorker 标识与共享父图执行对象

在每个 `GraphWorker-N.run` 帧中执行：

```python
(self._worker_id, self.name, self.ident, id(self._graph_execution))
```

实测示例：

```text
(2, 'GraphWorker-2', 132482678642240, 132482764816800)
(1, 'GraphWorker-1', 132482687034944, 132482764816800)
```

这四个值依次表示：

- `_worker_id`：`WorkerPool` 内部分配的 worker 编号；
- `self.name`：线程名；
- `self.ident`：线程标识；
- `id(self._graph_execution)`：父图执行对象 ID。

同一个 `WorkerPool` 创建的所有 `Worker` 共用一个 `graph_execution` 对象，所以多个线程得到相同的执行对象 ID 是正常现象。这个 ID 用于关联父图执行，不能区分其中的单个 worker。

选择标识时，按要确认的问题区分：

- 要区分单个 `GraphWorker`，用 `_worker_id`、`name`、`ident`；
- 要判断某个 iteration 子任务属于哪个父图执行，用 `id(self._graph_execution)`；
- 仅凭父图执行 id，不能把 `ThreadPoolExecutor-1_x` 精确绑定到某一个 `GraphWorker-N`。

在 `ThreadPoolExecutor-1_x` 的 `_execute_tracked_iteration_parallel` 帧中执行：

```python
import threading

(
    id(self.graph_runtime_state.graph_execution),
    threading.current_thread().name,
    threading.current_thread().ident,
)
```

若第一个值与某个 `GraphWorker` 的 `id(self._graph_execution)` 一致，说明该 iteration 属于同一个父图执行 / WorkerPool。

若需要进一步确认具体是哪一个 `GraphWorker-N` 提交了该 iteration，应查看线程栈：包含 `IterationNode._execute`、`_execute_parallel_iterations` 或 `concurrent.futures.wait()` 的那个 `GraphWorker`，就是等待这批子 Future 的父 worker。旧版本栈里可能仍显示 `as_completed()`。

实测补充：

- 在 `ThreadPoolExecutor-1_1` 的 iteration 子帧执行 `graph_engine.request_abort("debug child abort")` 后，`GraphWorker` 与 `ThreadPoolExecutor` 均退出；
- 这说明子图 abort 已经沿 `ChildGraphAbortedEvent` 传播到父 iteration，并触发兄弟子图收尾与 worker 退出；
- 该现象可作为 abort 传播链路生效的运行时证据，但不等于线程被强制杀死，线程是因为任务收尾后退出。

### 13.8 parallel iteration 有界等待修复（语义 A）

本节记录对旧 Future 架构的修复。官方最新版已经把 iteration 并行执行移到 engine/container handler，不再在 `iteration_node.py` 中使用 `as_completed()` / `future.result()`；本次没有做官方全量架构迁移，只借鉴其“显式完成状态、错误状态、usage 累加”的思路，在本地 Future 架构上封住无界等待。

修改位置：

- `src/graphon/nodes/iteration/iteration_node.py`
  - `_execute_parallel_iterations()`：主等待从 `as_completed()` 改为有界 `wait()`；executor 不再依赖上下文管理器隐式 `shutdown(wait=True)`。
  - `_drain_parallel_siblings()`：sibling drain 从逐个无超时 `result()` 改为整轮有界等待。
  - `_abort_future_indexes()` / `_abort_pending_parallel_iterations()`：共享 abort + cancel 逻辑。
  - `_fail_parallel_wait_timeout()` / `_fail_sibling_drain_timeout()`：统一记录超时事件并抛出领域异常。
- `src/graphon/nodes/iteration/exc.py`
  - 新增 `ParallelIterationWaitTimeoutError` 与 `ParallelSiblingDrainTimeoutError`，均继承 `IterationNodeError`，复用 `_run()` 已有的失败事件路径。

环境变量：

- `GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT`，默认 `300` 秒，控制主 iteration 等待。
- `GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT`，默认 `30` 秒，控制 abort 后 sibling drain。

非法值、非正数、非有限值都会回落到默认值。两个变量只控制业务执行预算，不改变诊断开关。

改动前后行为对比：

- 改动前：`as_completed()` 和 `future.result()` 可能无限等待；`ThreadPoolExecutor` 上下文退出还可能再次 `shutdown(wait=True)`；drain 中 `suppress(Exception)` 会吞掉异常并继续写 `sibling_wait_end`。
- 改动后：主等待和 drain 都有总预算；超时会先对 pending child 做协作式 abort/cancel，再让父 iteration 失败；executor 退出是 `shutdown(wait=False, cancel_futures=True)`。

语义 A 的边界：

- 父 iteration 超时后直接失败，不会把 pending future 当作成功完成。
- 未完成 index 不写 `sibling_wait_end`，只写 `sibling_wait_timeout`。
- pending index 的 usage 不合并；已经完成并合并的 usage 保留，不回滚，也不重复合并。
- 不强制杀死 Python 线程。未响应 abort 的子任务可能继续留在后台，直到自行结束或进程退出。
- `sibling_drain_end` 只在整轮 drain 正常完成时写；超时写 `sibling_drain_timeout`。

新增事件字段：

- `parallel_wait_timeout`：`pending_indexes`、`pending_count`、`timeout_seconds`、`elapsed_seconds`。
- `sibling_wait_timeout`：`iteration_index`、`abort_requested`、`cancelled`、`child_engine_id`、`child_execution_id`。
- `sibling_drain_timeout`：`pending_indexes`、`pending_count`、`completed_indexes`、`merged_indexes`、`timeout_seconds`、`elapsed_seconds`。
- `parallel_pending_abort` / `child_abort_targeted`：通过 `propagation="iteration_wait_timeout"` 区分主等待超时触发的 abort。

测试：

- `tests/nodes/test_iteration_parallel_abort.py`
- `tests/nodes/test_iteration_parallel_timeout.py`

测试全部使用 `Future`、`MagicMock` 和本地 fake executor，不连接真实数据库、Redis、模型服务或外部网络。
