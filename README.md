# T22 门诊护栏规则

软件设计与开发课程大作业：门诊前台、医生和门店管理人员使用的诊疗护栏系统。宠物主人不登录。仓库包含需求与设计、Django/DRF 后端、Web 前端、测试和验收材料。

## 目录

| 路径 | 内容 |
| --- | --- |
| `docs/` | 全部课程文档，按原课程编号组织：来源、需求、设计、AI 留痕、验收证据和提交清单 |
| `backend/` | Django/DRF 应用、迁移、Python 依赖和后端测试 |
| `frontend/` | Web 工作台 HTML、CSS、JavaScript 和前端测试 |
| `.github/` | PR 模板和 GitHub Actions CI |

仓库保存代码、Markdown 正文和可复核证据。Word 只在最终提交包中生成/保存，不与 Markdown 源稿并列提交到 GitHub；本地生成的 `.docx` 与提交包已由 `.gitignore` 排除。临时日志、虚拟环境、数据库、凭据和原始运行产物也不纳入版本库。

## 从这里开始

- [作业导航](docs/00_作业导航.md)
- [需求规格说明书](docs/03_需求规格说明书.md)
- [架构说明](docs/04_设计与控制文件/architecture.md)
- [后端运行说明](backend/README.md)
- [测试与实测报告](docs/06_测试与实测报告.md)
- [问题、尝试与测试阶段总结](docs/10_验收证据/19_后端综合验收/32_当前问题与测试总结.md)
- [最终提交文件清单](docs/11_最终提交候选/00_成果与文件清单.md)
- [仓库协作规范](AGENTS.md)
- [目录与 Git 流程参考分析](docs/00_来源摘录/07_仓库组织与Git工作流参考.md)

## 仓库使用约定

这是一个前后端协同的课程单仓库：应用源码按职责放在 `backend/` 与 `frontend/`，课程材料集中在 `docs/` 并保留课程编号。Markdown 是唯一维护版本，Word 只在最终提交包中生成。

日常开发从 `main` 拉出一个短期功能/修复/文档分支，经 PR 与 CI 检查后合并；合并后删除分支。项目规则、文件位置和命名口径见 [AGENTS.md](AGENTS.md)。

## 开发与验证

需要 Python 3.12、MySQL 8.4；真实队列集成还需要 RabbitMQ 4.2。按 `backend/requirements.lock` 安装 Python 依赖。凭据和数据库配置须保存在仓库外。详细安装、配置与命令见[后端运行说明](backend/README.md)。

```powershell
cd backend
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test vetcontrols governance clinic tests --noinput -v 2
```

GitHub Actions 在隔离的 MySQL/RabbitMQ 服务中运行回归。正式负载测试使用独立合成数据与服务器；短时预检、课堂活跃会话结果和持续高在途结果是不同口径，不能互相替代。

## 当前验收状态

核心前后端功能、数据库持久化、权限与审计、告警队列、三类导出及相关回归已有验收记录，详见测试报告。持续 360 在途、完整混合业务负载下的 280 ms P95 仍未通过；最新R24新代码诊断完成132685个窗口内发起请求、零业务失败及40007次导出/8744186个审计目标核对；发送端组内存峰值降至约2.53GB，但32核CPU仍接近100%，七类P95为369—1296ms；当前结果和下一步见[实施计划与逐轮记录](docs/10_验收证据/19_后端综合验收/33_严格性能改造实施计划.md)。不将短时查询预检描述为正式验收结果。

需求、权限和验收口径变更按需求基线记录；代码修改通过短期分支、Pull Request 和 CI 进入 `main`。PR 合并后自动删除来源分支；关闭但未合并的分支须确认不再需要后再清理。本仓库不包含课程原始文件、私密配置或完整云端运行数据库。
