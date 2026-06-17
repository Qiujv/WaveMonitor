# IPC 写入策略说明

记录 WaveMonitor 客户端发送消息时调用了 `_IOWorker._handle_write` 会根据平台采取不同的策略：

- macOS / POSIX：使用 `write_native_message`。
- Windows：优先使用 `write_message`，失败时再 fallback 到 `write_native_message`。

这样 client 侧 `_IOWorker` 发消息的速度最快，也就是 close 之前把 queued messages 全部发出去的耗时。

## 两种写入方法

WaveMonitor 当前有两条 client-to-server IPC 写入路径。

`write_message(sock, msg)` 使用持久 `QLocalSocket`：

- client worker 持有一个 `QLocalSocket`。
- 每条消息通过 `QLocalSocket.write(...)` 写入。
- 写完后调用 `flush()`，必要时调用 `waitForBytesWritten()`。
- Windows 上这条路径由 Qt 封装 named pipe。

`write_native_message(server_name, msg)` 使用平台原生端点：

- 每条消息单独打开连接，写入完整 frame，然后关闭。
- Windows 上直接写 `\\.\pipe\<name>`。
- macOS / POSIX 上直接使用 Unix domain socket。

benchmark 显示每次 native socket 重连耗时在 50 us 以内。相比当前讨论中的几十 ms
差异，这个成本可以忽略。

## 性能对比

已有 benchmark 结果如下：

| 平台和路径 | `IOWorker.run` | `pack_message` | write period | 结果 |
| --- | ---: | ---: | ---: | --- |
| Windows `write_message` with `QLocalSocket` | 1.5 s | 25 ms | 10 ms + 10 ms | normal ending |
| Windows `write_native_message` with named pipe | 2.5 s | 25 ms | 75 ms | normal ending |
| macOS `write_message` with `QLocalSocket` | 2.8 s | 5 ms | 2 ms + 24 us | close 前有长时间 `waitForBytesWritten` |
| macOS `write_native_message` with socket | 1.9 s | 5 ms | 73 ms | normal ending |

这里的 write period 分别对应：

- `QLocalSocket.write(...)` 加 `QLocalSocket.waitForBytesWritten(...)`
- `_write_windows_named_pipe(...)`
- `socket.sendall(...)`

结论：

- macOS 上 `write_native_message` 更快。
- Windows 上 `write_message` 更快。

## macOS 为什么使用 native socket

macOS 上 `write_native_message` 直接走 Unix domain socket，路径很薄，整体
`IOWorker.run` 时间更短。

相反，macOS 上使用 `QLocalSocket` 时，之前已经验证过一个问题：消息看起来能写入，
`simple_usage.py` 和 `bench_client.py` 也能跑通，但实际会出现所有消息拖到 close
阶段才集中发出的现象。benchmark 里也能看到，`write_message` 的单次 write period
看起来很短，但 close 前会出现较长的 `waitForBytesWritten`。

因此 macOS 上不能只看每次 `QLocalSocket.write(...)` 的局部耗时。对 client worker
来说，更重要的是整批队列什么时候真正发完。按这个标准，macOS 应该使用
`write_native_message`。

## Windows 为什么优先使用 QLocalSocket

Windows 上结果相反：`write_message` 的整体 `IOWorker.run` 时间明显短于
`write_native_message`。

这里更可能的原因是 Qt 的 `QLocalSocket` Windows backend 对 named pipe 做了更合适的
缓冲或异步处理。相比之下，当前 `_write_windows_named_pipe(...)` 是同步
`os.write(...)`，大 payload 写入时更直接地暴露 pipe 背压，因此单次 native write
会更慢。

Windows 上真实手动验证也显示，只使用 `QLocalSocket` 时，多客户端发送和 server
restart 的实际功能都正常。因此当前策略是：

- Windows 默认使用 `write_message`，拿到更好的 client worker 消费速度。
- 如果 `write_message` 抛出 `RuntimeError`，再 fallback 到 `write_native_message`，
  保留 named pipe 原生写入作为兜底。

## 测试修复和小彩蛋

这次修复前，`tests/test_client_ipc.py` 使用测试内置的 `ProbeServer` 模拟
`QLocalServer`。`ProbeServer` 没有运行在真实窗口进程的 Qt event loop 中，而是靠
pytest 主线程里的 `processEvents()` 和手动 poll 推进事件。这个模型在 Windows 上会让
`QLocalSocket` 路径出现 test fail，但手动验证真实 server 时功能正常。

因此测试改成启动真实 `start_wave_monitor()` server。为了让真实 server 的处理结果可
观察，`DataSource` 增加了一个隐藏消息：

```python
{"_type": "_set_ipc_probe_value", "value": 123}
```

server 收到后会把 `value` 写入现有 `ipc.state_memory` 的第二个槽位。测试侧通过
`ClientStateMemory.get_ipc_probe_value()` 读取这个值，确认真实 server 已经处理了
消息。这个隐藏消息没有暴露在公开 client API 里，可以看作一个常驻产品里的小彩蛋。
