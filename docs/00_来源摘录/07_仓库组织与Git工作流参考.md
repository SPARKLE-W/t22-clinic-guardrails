# 仓库组织与 Git 工作流参考

**用途：** 记录本项目目录与分支规范的参考依据和采用理由。外部文章用于取舍，不直接替代项目约束。

## 本项目结论

T22 保留一个私有单仓库，让前后端、接口契约、课程设计和回归测试能在同一 PR 中一起审查。对照 GitHub 全栈仓库讨论和 Nx 的目录原则，单仓库内部仍按本项目的实际职责分层：`backend/` 放 Django/DRF 服务，`frontend/` 放浏览器工作台，`docs/` 放课程文档、设计、AI留痕和验收证据。课程编号继续用于 `docs/` 内分类，不把编号放在产品源码目录名里。没有采用 Nx 的 `apps/`、`libs/`，因为当前只有一个服务和一个工作台，没有共享库拆分需求。

Git 采用 GitHub Flow 式的单长期主分支：`main` 加短期主题分支，通过 PR 和 CI 合并。当前是小型课程项目，没有多个并行产品版本、固定发布列车或生产热修复线；常设 `develop`、`release`、`frontend/dev`、`backend/dev` 会增加同步和合并成本。独立前后端改动可各自开短期分支；需要一起改时在一个 PR 中提交，避免人为把接口联调拆成两个不一致的基线。

提交按课程编号和领域归档，不因文件类型再复制一份。Markdown 是源文件；提交 Word 只进入本地最终提交包。过程审查快照仅在确需保存评审时保留，日常更新改当前稿，防止多份相同文件长期漂移。

## 参考来源与取舍

| 来源 | 可借鉴内容 | 对本项目的取舍 |
| --- | --- | --- |
| [GitHub Community：全栈仓库结构讨论](https://github.com/orgs/community/discussions/190870) | 讨论单仓库与多仓库及前后端协作；回答者认为多数全栈应用可先用单仓库，让跨端改动在同一 PR 协调。 | 作为实践讨论而非规范。T22 的前后端共同交付、同套验收与接口联调支持单仓库；没有独立部署/权限需求证明要拆仓。 |
| [Nx：Monorepo Folder Structure](https://nx.dev/docs/kb/folder-structure) | 目录结构是约定而非硬性要求；按职责组织，复用代码确实被多个项目消费后再抽共享层；避免过深嵌套。 | 采纳按职责分开放置后端、前端和文档；不照搬 Nx 的 `apps/`、`libs/`，因为当前没有多应用或共享库的实际需求。 |
| [Atlassian：Gitflow Workflow](https://www.atlassian.com/en/git/tutorials/comparing-workflows/gitflow-workflow) | Gitflow 用 `develop`、`feature`、`release`、`hotfix` 支持阶段化发布与维护多个版本。 | 当前课程项目没有多版本发布和长期稳定分支需求，不采用完整 Gitflow；仅保留主分支和短期工作分支。 |
| [中培伟业：浅论选择分支策略](https://www.zpedu.com/it/xxjl/4228.html) | 说明 Git 本身不强制指定一种分支策略，分支是团队约定；文章介绍了主干、开发和热修复分支的用途。 | 作为中文入门材料。分支策略按项目发布方式选取；T22 无需照搬多长期分支。截图里提及的 zpedu 对比文未能由标题唯一定位，后续如需逐篇评价应补原链接。 |
| [GitHub Docs：GitHub Flow](https://docs.github.com/en/get-started/using-github/github-flow) | 官方轻量工作流：主题分支、PR、讨论与检查，符合单一主线的协作方式。 | 采用其简化流程：`main` → 短期分支 → PR/CI → `main` → 删除来源分支。 |
| [Trunk Based Development：Short-Lived Feature Branches](https://trunkbaseddevelopment.com/short-lived-feature-branches/) | 允许短期分支供评审和 CI 使用，集成后及时删除；分支等待审查的时间也算在生命周期中。 | 采用短生命周期原则；本项目以 PR/CI 作为 main 保护门，不照搬高频部署团队的直接提交模式。 |
| [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) | `type(scope): description` 的可识别提交格式，可供人和工具阅读。 | 作为提交信息风格，不用它推断实际工作或自动改写历史。 |

## 执行检查

- 新目录/文件先确认当前分类没有合适位置；新建后同步更新 README 或对应导航。
- 路径调整前搜索仓库内引用，移动后核对相对链接。
- PR 合并并删除分支后，定期清理已关闭、确认不再需要的分支；保留未审查的独立提交，直到确认其归宿。
- 每次提交仅维护一份当前文档源；生成的 Word/PDF 和本地提交 ZIP 保持在被忽略的输出目录。
