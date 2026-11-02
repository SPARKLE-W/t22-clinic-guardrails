# Git与GitHub接入记录

> 2026-10-22；用户要求把Git等操作加入开发流程并接入其GitHub。由主代理代操作，不计作用户手工修改或他人审查。

本目录单独建仓库，远程为用户SPARKLE-W名下私有仓库。首次提交如实导入当前已有成果，不重建虚构历史。后续采用功能分支、相关测试、批末全量回归、PR和正常合并；具体职责见02流程§4.3。

`.gitignore`排除实际口令、数据库、虚拟环境、缓存、原始课程文件和大量运行原始结果。保留当前需规Word和Markdown、设计/代码、AI留痕、历史审查摘要/快照与精选最新回归证据。本地原件和原始输出不删除；远程仓库不是完整课程备份。`.gitattributes`保留已有文件字节，避免冻结基线因换行转换变化。

CI配置使用Python3.12、MySQL8.4.13临时服务，安装锁定依赖，执行框架、迁移漂移和完整回归。服务密码是公开的临时CI值，只用于该隔离服务；Django密钥每次随机生成，配置写runner临时目录。官方checkout v5及setup-python v6在接入时通过GitHub API核对提交指纹后固定。

本轮先前直接访问GitHub API发生TLS超时；读取本机已配置的系统代理后，仅对本次gh进程和本仓库Git使用该代理，不修改全局代理。已核对CLI登录账号SPARKLE-W；本机Git提交身份仅配置在本仓库，使用GitHub noreply地址，不改全局身份。连接器写入的提交身份由GitHub账号配置决定。

## 实际执行与结果

- 私有仓库：[SPARKLE-W/t22-clinic-guardrails](https://github.com/SPARKLE-W/t22-clinic-guardrails)。首次真实导入提交`152bdde6de08fbf7e405af69786fad8ded481006`，226个已跟踪文件；没有上传其他平时作业。
- 本机新跑59项测试通过，框架检查及迁移漂移检查通过。实际结果见10目录本阶段的`baseline_01.json/.txt`。提交前逐文件检查暂存内容，未发现已知私密值或禁止上传项；需规MD、Word和冻结快照字节保持一致，原始任务书本机指纹不变。
- 命令行第一次推送CI被拒，原因是原有OAuth授权没有`workflow`。已有GitHub连接器可写工作流，核对同一用户后通过它建立提交`3578527`及修正`ccce1b2`，拉取比较内容后衔接本地分支。未强推；被拒的本地提交仅留本地`archive/workflow-cli-attempt`作恢复记录。随后用户明确要求立即补齐缺失权限，已通过GitHub设备授权完成CLI的`workflow`扩展，实际核验作用域为`gist/read:org/repo/workflow`。未向文档复制访问令牌或设备验证码。
- 首次CI在任务启动前失败：在job层env引用了该位置不支持的`runner`上下文。按系统排错技能核对[GitHub官方上下文可用范围](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability)后，改用包含运行编号的`/tmp`路径。失败运行`39742142698`保留；修正后的PR运行[39742382994](https://github.com/SPARKLE-W/t22-clinic-guardrails/actions/runs/39742382994)实际成功，59项MySQL测试通过，框架/迁移检查通过。没有以配置完成替代运行成功。
- [PR #1](https://github.com/SPARKLE-W/t22-clinic-guardrails/pull/1)承载CI与开发流程接入；[Issue #2](https://github.com/SPARKLE-W/t22-clinic-guardrails/issues/2)登记下一批正式病历写入与对应运行护栏的范围及完成标准。
- 用户看到main保护提示后要求检查，已实际启用：只经PR更新、必须通过来自GitHub Actions的`regression`检查且分支最新、禁止强推/删除、解决未完成讨论；管理员同样受约束。批准人数为0，不制造个人仓库无法满足的他人审批要求。第一次API请求因同时传旧contexts和新checks字段返回422，原始失败保留；改用checks后成功，并回读确认。不存在套餐限制导致保护未生效的情况。

阶段证据集中在`10_验收证据/10_Git与GitHub流程接入/`。本轮没有调用子代理；没有修改产品角色/权限/红线或冻结需求，没有执行正式性能验收。本机MySQL回归后已关闭。
