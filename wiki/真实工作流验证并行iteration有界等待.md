# 真实工作流验证：并行 iteration 有界等待

> 适用改动：`GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT`、`GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT`
> 源码根：`/home/jackson/graphon/src/graphon`
> 验证对象：ai-dify 中实际运行的 Celery worker 和工作流，不依赖 pytest 桩

这份文档只回答一个问题：跑真实 workflow 时，怎么判断超时修复已经生效，以及 usage 有没有被错误合并。

## 1. 先确认跑的是新代码

改完源码、推到私有仓库、更新 `uv.lock` 并 `uv sync` 之后，先查虚拟环境里的 graphon，不要直接跑 workflow。

```bash
cd /home/jackson/ai-dify/api

uv run python -c "import graphon.nodes.iteration.iteration_node as m; print(m.__file__)"

grep -n "parallel_wait_timeout" \
  .venv/lib/python3.12/site-packages/graphon/nodes/iteration/iteration_node.py

grep -n "sibling_drain_timeout" \
  .venv/lib/python3.12/site-packages/graphon/nodes/iteration/iteration_node.py

cat .venv/lib/python3.12/site-packages/graphon-0.4.0.dist-info/direct_url.json
```

能看到下面三点，才继续：

- 导入路径是当前 ai-dify 虚拟环境使用的 graphon；
- `iteration_node.py` 里有 `parallel_wait_timeout` 和 `sibling_drain_timeout`；
- `direct_url.json` 里的 commit id 是刚推上去的私有仓库提交。

如果只改了 `/home/jackson/graphon/src/graphon`，但 ai-dify 还没同步，worker 仍会跑旧包。这种情况下看不到新事件是正常的，不能据此判断修复无效。

## 2. 给 Celery worker 加环境变量

诊断事件和超时变量都要落在真正执行 workflow 的 Celery worker 上。终端 `export` 只影响当前 shell；VSCode / CodeBuddy compound 里的 Flask 和 Celery 是两个进程，`env` 不共享。

启动 worker 时可以直接绑定：

```bash
cd /home/jackson/ai-dify/api

env \
  GRAPHON_TRACE_FILE=/home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl \
  GRAPHON_TRACE_SIGNAL=SIGUSR1 \
  GRAPHON_TRACE_QUEUE_SIZE=10000 \
  GRAPHON_TRACE_FLUSH_TIMEOUT=0.5 \
  GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT=300 \
  GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT=30 \
  uv run celery -A app.celery worker -P solo <其余参数不变>
```

IDE 里就写进 `Celery Worker (Debug)` 的 `env`：

```jsonc
"env": {
  "GRAPHON_TRACE_FILE": "/home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl",
  "GRAPHON_TRACE_SIGNAL": "SIGUSR1",
  "GRAPHON_TRACE_QUEUE_SIZE": "10000",
  "GRAPHON_TRACE_FLUSH_TIMEOUT": "0.5",
  "GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT": "300",
  "GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT": "30"
}
```

启动后查一次进程环境，确认没有写错进程：

```bash
tr '\0' '\n' < /proc/<celery-worker-pid>/environ | grep '^GRAPHON_'
```

真实复现时再把超时调小。比如主等待用 `5` 秒，drain 用 `5` 秒。不要长期把生产 worker 的默认预算调低；验证完恢复默认值。

## 3. 准备 workflow

用同一个并行 iteration 节点做四组验证。

建议结构：

- iteration 打开 parallel；
- 至少 3 个 item；
- 其中一个 item 很快完成；
- 一个 item 可控地变慢；
- 一个 item 用来触发 child abort 或卡住；
- 每个子图里放一个会产生 LLM usage 的节点，方便看 usage 合并是否正确。

慢节点优先用可控的外部 HTTP 延时接口。LLM 节点也能造慢调用，但耗时受供应商影响，不够稳。

验证主等待超时和 drain 超时时，超时值要小，慢节点要明显大于这个值。比如超时设 5 秒，慢节点至少 20 秒。

## 4. 场景 A：正常 workflow，先做回归

这一步看修复有没有把正常路径弄坏。

设置：

```bash
GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT=300
GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT=30
```

跑一个所有 child 都在预算内完成的并行 iteration。

预期：

- workflow 成功；
- iteration 输出数量正常；
- 父节点 usage 包含各 child 的 token；
- 日志里没有 `parallel_wait_timeout`、`sibling_wait_timeout`、`sibling_drain_timeout`；
- 没有 workflow 卡住。

日志检查：

```bash
grep -n '"event": "parallel_wait_timeout"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl

grep -n '"event": "sibling_drain_timeout"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl
```

没有输出就对了。这个场景只能证明没有明显回归，不能证明超时修复生效。

## 5. 场景 B：主等待超时

这一步验证旧的 `as_completed()` 无界等待已经被有界等待替代。

设置：

```bash
GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT=5
GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT=30
```

工作流里让至少一个 child 跑超过 5 秒，最好跑到 20 秒以上。提交 workflow 后看接口或页面上的节点状态。

预期：

- iteration 在大约 5 秒后失败，不会一直转圈；
- 失败信息里能看到 `ParallelIterationWaitTimeoutError`；
- 日志里有 `parallel_wait_timeout`；
- `pending_indexes` 是当时没完成的 item 下标；
- `elapsed_seconds` 接近 5 秒，允许一点调度误差；
- 紧跟着有 `child_abort_targeted`，其中 `propagation` 是 `iteration_wait_timeout`；
- 对仍在运行的 child，`future_cancel_result` 的 `cancelled` 可能是 `false`，这是正常的，运行中的 Future 不能靠 `cancel()` 停下。

检查命令：

```bash
grep -n '"event": "parallel_wait_timeout"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl

grep -n '"propagation": "iteration_wait_timeout"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl
```

如果超过 5 秒很久仍没有失败事件，先查：

- worker 是否真的拿到了新的环境变量；
- 卡住的位置是不是在这个 parallel iteration 之前；
- 慢 child 是否其实早就完成；
- 运行的是不是旧 graphon 包。

这个场景不要求后台慢任务立刻消失。语义 A 允许它继续跑到自己结束，父 iteration 不再无界等它。

## 6. 场景 C：child abort 后，sibling drain 正常收尾

这一步验证 drain 仍然会合并已完成 child 的 usage。

准备一个会触发 `GraphRunAbortedEvent` 的 child。真实 workflow 里如果没有稳定的业务节点能触发，可以用断点注入：在 `_execute_tracked_iteration_parallel()` 帧执行 `graph_engine.request_abort("debug child abort")`。别在 `GraphWorker-N.run` 帧调用 `self.request_abort()`，那里没有这个方法；也别在普通 `ThreadPoolExecutor` 帧直接引用 `graph_engine`。

设置：

```bash
GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT=300
GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT=10
```

其他 child 收到 abort 后应在 10 秒内收尾。

预期：

- 父 iteration 失败，原因是 child abort；
- 有 `sibling_drain_begin`；
- 每个非 cancelled sibling 有 `sibling_wait_begin` 和 `sibling_wait_end`；
- 已完成的 sibling 有 `usage_merge_done`；
- 最终有 `sibling_drain_end`；
- 没有 `sibling_drain_timeout`；
- 没有 `parallel_wait_timeout`。

检查：

```bash
grep -nE '"event": "(sibling_drain_begin|sibling_wait_end|usage_merge_done|sibling_drain_end)"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl
```

这个场景的重点是 usage。修复后 drain 仍然等已完成 sibling 入账，只是等待有预算。不能因为加了 timeout 就把已经开始合并的 usage 丢掉。

## 7. 场景 D：child abort 后，drain 超时

这一步验证真正卡住时父 iteration 能失败，而且不会伪造完成。

设置：

```bash
GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT=300
GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT=5
```

让一个 child 触发 abort，再让另一个 sibling 不响应 abort，持续超过 5 秒。慢节点可以用可控 HTTP 延时，或者调试器暂停线程。

预期：

- 父 iteration 在大约 5 秒后失败；
- 失败信息里能看到 `ParallelSiblingDrainTimeoutError`；
- 有 `sibling_wait_timeout`，其中 `pending_indexes` 或对应 index 指向没完成的 child；
- 有 `sibling_drain_timeout`；
- pending index 没有 `sibling_wait_end`；
- pending index 没有 `usage_merge_done`；
- 没有 `sibling_drain_end`；
- 已完成 sibling 的 usage 仍然保留；
- worker 进程还能继续处理新任务。

检查：

```bash
grep -n '"event": "sibling_wait_timeout"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl

grep -n '"event": "sibling_drain_timeout"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl

grep -n '"event": "sibling_drain_end"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl
```

最后一条不该出现。出现 `sibling_drain_end` 说明这轮 drain 被当成正常完成，和语义 A 不符。

## 8. 日志字段怎么看

一次超时记录大概按这个顺序出现：

```text
sibling_drain_begin
sibling_wait_begin
future_cancel_attempt / future_cancel_result
sibling_wait_timeout
sibling_drain_timeout
```

主等待超时则是：

```text
parallel_wait_timeout
child_abort_targeted  propagation=iteration_wait_timeout
future_cancel_attempt / future_cancel_result
```

重点字段：

- `pending_indexes`：没完成的下标。确认它指向自己故意弄慢的那个 item。
- `elapsed_seconds`：等待耗时。应该接近配置值，不会精确到毫秒。
- `cancelled`：运行中 Future 的 cancel 结果经常是 `false`，这不是缺陷。
- `abort_requested`：说明超时前有没有发过 abort。
- `merged_indexes`：已经合并 usage 的数量。pending index 不应出现在已合并路径里。

按 session 过滤更省事。先从 `trace_configured` 取 `session_id`，再筛同一次 workflow：

```bash
grep '"session_id": "<本次 session>"' \
  /home/jackson/graphon/debug/graphon_trace.realworkflow.jsonl
```

## 9. 判定表

| 场景 | 关键事件 | 结果 |
|---|---|---|
| 正常回归 | 没有新增 timeout 事件，workflow 成功 | 正常 |
| 主等待超时 | `parallel_wait_timeout`，`ParallelIterationWaitTimeoutError` | 修复生效 |
| drain 正常收尾 | `sibling_wait_end`、`usage_merge_done`、`sibling_drain_end` | usage 合并未受影响 |
| drain 超时 | `sibling_wait_timeout`、`sibling_drain_timeout`，无 `sibling_drain_end` | 语义 A 生效 |
| 超时后仍无限挂住 | 超过预算很多仍无失败事件 | 代码未加载，或卡点不在本次修复范围 |
| pending index 出现 `usage_merge_done` | 未完成项被合并 usage | 语义不符，需要停下来查 |

## 10. 容易误判的地方

**运行中的 Future 没被 cancel 掉。** `future.cancel()` 只能取消还没执行的任务。已经跑起来的 child 要靠协作式 abort。cancel 返回 `false` 不代表 abort 没发出去，要看 `child_abort_targeted`。

**后台线程还活着。** 语义 A 只保证父 workflow 不再无限等待。Python 不能安全强杀线程，未响应 abort 的 child 可能继续跑到自己结束。这不等于修复失败。

**没看到 drain 事件。** 没有 child abort 时不会进入 `_drain_parallel_siblings()`。正常 workflow 没有 drain 事件是正常的。

**超时值调太小。** 主等待 5 秒、drain 5 秒只适合隔离复现。真实业务里若正常耗时靠近这个值，会误伤正常任务。

**只看页面状态。** 页面只显示 workflow 失败。要确认是不是本轮修复，必须看事件字段和异常类型。

**拿抓栈窗口当产品 SLA。** 之前诊断方案里的 5 秒、30 秒观察窗口只是人工排查用。真正控制失败时机的是 `GRAPHON_PARALLEL_ITERATION_WAIT_TIMEOUT` 和 `GRAPHON_PARALLEL_SIBLING_DRAIN_TIMEOUT`。

## 11. 查阅位置

- `src/graphon/nodes/iteration/iteration_node.py`：主等待 `L279-L372`，abort helper `L505-L592`，主等待超时 `L594-L620`，drain `L622-L731`，drain 超时 `L733-L773`
- `src/graphon/nodes/iteration/exc.py`：`L32-L53`
- `wiki/子图Abort传播与线程退出诊断方案.md`
- `wiki/并行迭代取消与命令消费修复.md`
