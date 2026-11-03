# TBR-05 负载与证据工具

协议源：[正式负载测量协议 v1.0](../../../../docs/10_验收证据/19_后端综合验收/01_正式负载测量协议_v1.0.md)。这些工具由此前本地部署缓存中的正式负载脚本纳入版本管理；纳入不代表当前源码已经完成云端验收。

| 文件 | 用途 |
| --- | --- |
| `01_seed_formal_data.py` | 向空的 `t22_perf_` 隔离库写固定种子夹具，凭据输出到仓库外 |
| `02_formal_service.py` | 启动独立 Waitress 实例，可选应用/SQL/排队计时 |
| `03_tbr05_load.py` | 真实会话、完整响应体计时、请求与资源 JSONL；默认360并发、300秒预热、600秒正式窗口。HTTP在途数在完整体接收并关闭连接后结束；CSV核验由有界队列另行处理，不把核验时间计入在途 |
| `04_sample_windows_resources.ps1` | 应用服务器 Windows CPU、进程与磁盘采样 |
| `05_align_resource_evidence.py` | 按 UTC 分钟对齐请求与服务器资源，只用于关联分析 |
| `06_evaluate_tbr05.py` | 从原始请求重算分项P95、逐分钟吞吐、失败、导出完整性及在途采样门槛 |

运行负载端需要 `requests`、`psutil`，服务端另需后端锁定依赖及 Waitress。每轮登记实际版本、源码提交与工具哈希。不要向仓库提交账号文件、原始请求、运行配置或日志。

云端负载只在正式复测窗口执行；正式命令固定 `--concurrency 360 --warmup-seconds 300 --formal-seconds 600`，不设置 `--only-group`。`--base` 指向预检确认的应用内网地址。正式前完成独立库、迁移、夹具计数、进程/连接预算与资源采样预检。默认参数只是候选窗口标签，不能当成通过结论。

发送端对病历更正和采纳响应立即更新版本引用；导出与其他响应由八个核验线程复核完整字节、CSV行数与摘要。有界队列若积压，会阻塞发起线程并使实际HTTP在途下降，必须视为客户端容量问题，不能用登录账号数或核验排队数补足360在途。

判定命令：`python 06_evaluate_tbr05.py --summary <prefix.summary.json> --requests <prefix.requests.jsonl> --inflight <prefix.inflight.jsonl> --output <仓库外新报告.json>`。退出0只表示自动检查项满足；完整验收还需人工复核环境、数据、固定混合比例、资源/错误日志、完整范围导出与峰值内存，以及从请求区间重建并发。采样不能证明采样点之间从未掉并发。大小分层报告分位，不擅自追加协议未明确冻结的分层P95硬门槛。

本地工具验证：`python -m unittest discover -s backend/tests/performance/tbr05 -p "test_*.py" -v`。该命令使用合成证据，不连接数据库或云服务器。
