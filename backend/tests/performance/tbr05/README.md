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

云端负载只在正式复测窗口执行；正式命令固定 `--concurrency 360 --warmup-seconds 300 --formal-seconds 600`，不设置 `--only-group`。应用池测试用重复 `--target-base` 指向各个应用内网端口；`--base` 仅作单端口诊断的后备地址。正式前完成独立库、迁移、夹具计数、进程/连接预算与资源采样预检。默认参数只是候选窗口标签，不能当成通过结论。

在应用服务器的 `backend` 目录中，Waitress 候选进程由 `backend/运行脚本/02_正式负载应用池.ps1` 管理。启动前在管理员 PowerShell 设置 `T22_FORMAL_LOAD=1`、隔离库配置路径、Python 路径及包含当前内网地址的 `T22_ALLOWED_HOSTS`，再显式传入 `-BindAddress`；脚本会校验隔离库名、8×48 连接预算、候选配置和端口，并在 `C:\ProgramData\T22\formal-load` 保存进程状态与日志。启动命令为 `.\运行脚本\02_正式负载应用池.ps1 -Action start -BindAddress <当前应用机内网IPv4>`，停止命令为 `.\运行脚本\02_正式负载应用池.ps1 -Action stop`。不要照抄旧候选中的内网地址；状态和日志不进仓库。

发送端对病历更正和采纳响应立即更新版本引用；导出与其他响应由八个核验线程复核完整字节、CSV行数与摘要。有界队列若积压，会阻塞发起线程并使实际HTTP在途下降，必须视为客户端容量问题，不能用登录账号数或核验排队数补足360在途。

判定命令：`python 06_evaluate_tbr05.py --summary <prefix.summary.json> --requests <prefix.requests.jsonl> --inflight <prefix.inflight.jsonl> --output <仓库外新报告.json>`。报告并列给出0.5秒在途采样与完整请求区间重建的并发最值；两种证据都必须覆盖全窗且恰为360，否则自动判定失败。退出0只表示自动检查项满足；完整验收还需人工复核环境、数据、固定混合比例、资源/错误日志、完整范围导出与峰值内存。大小分层报告分位，不擅自追加协议未明确冻结的分层P95硬门槛。

应用池有8个独立端口；发压时每个端口都必须作为一个目标传入，不能只用 `--base`，否则其他进程收不到负载。Linux发送机可用 `args=(); for port in $(seq 18930 18937); do args+=(--target-base "http://${APP_IP}:${port}"); done` 生成参数，再将 `"${args[@]}"` 传给 `03_tbr05_load.py`。360个发压线程会按工作线程编号均分到目标，摘要记录目标数和每个目标分配的线程数；旧 `--tunnel-base` 仍可使用。本候选为8进程×48线程、MySQL上限512，以360并发下每端口45个客户端为起点，最终须由队列/CPU/DB实测确认，不视为已验收。

本地工具验证：`python -m unittest discover -s backend/tests/performance/tbr05 -p "test_*.py" -v`。该命令使用合成证据，不连接数据库或云服务器。
