# 受限完整CSV导出接口

> 2026-10-23，开发批次18。依据冻结需规FR-07.4/FR-08、C-05/C-08、UC-08/09与既定权限投影。本文规定具体路由和CSV列；在PR合并并应用迁移0008前，它是待部署契约。

## 通用约定

三类接口均为同源会话 `POST` JSON，须有CSRF，不要求Idempotency-Key。每次成功下载是一次新的读取，均重新核验当前账号/门店及范围，并在数据库事务内完整生成UTF-8 CSV字节、登记ExportRequest、写AuditEvent，必要时关联AuditTarget；事务成功后才构造文件响应。断连后服务端只能证明文件就绪，不能证明浏览器完整接收或用户保存到磁盘；再次下载重新授权和审计，不沿用旧文件权限。

成功响应 `Content-Type: text/csv; charset=utf-8`、`Content-Disposition: attachment`、`Cache-Control: no-store`、`Content-Length`、`X-Byte-Count`、`X-Content-SHA256`、`X-Export-Ref`、`X-Record-Count`。前端获取完整字节后核对长度与SHA-256，匹配才能提示“已完整接收”；断连、长度或摘要不符显示“未完整接收/结果待核对”。服务端`ready`只表示已生成且必要审计持久化，不能标为客户端已收妥。CSV第一行为列名，CRLF换行，不截断授权集合。`X-Record-Count`在病历为病历数，在命中类为命中数；CSV行数另记ExportRequest.row_count，可能与病历/命中数不同。响应失败为JSON错误，不返回CSV字节。

时间范围若出现，统一为带时区的`{"from":"...","to":"..."}`，左闭右开。病历按接诊建档时间；命中按校验发生时间。医生先按当前账号负责接诊且本店过滤，再应用时间/规则/状态。任何来自客户端的门店、医生、宠物、诊断或命中值筛选字段均拒绝；未提供筛选时导出该角色全部获准范围。

## 医生病历：`POST /api/exports/clinical-records`

权限`export.clinical`。请求必需且仅有`scope`和非空`purpose`。scope二选一：`{"record_refs":["UUID",...]}`或`{"time_range":{"from":"2026-10-23T00:00:00+08:00","to":"2026-10-24T00:00:00+08:00"}}`。显式集合不能空或重复，任何一个编号不存在、跨店或不属于当前医生则整次403，不静默删除其余行。时间范围可真实返回空集合，仅输出表头。

列顺序：`record_ref,encounter_ref,template_version_ref,field_meanings,diagnosis,examination,extra_fields,adopted_content,responsibility_metadata,medication_chain,clinical_versions,error_mark,related_encounter_refs`。每病历一行；复杂链按JSON文本置于CSV单元格。`extra_fields`为现版可选模板扩展值（无值时为`{}`），`clinical_versions`逐版保留扩展值。保留各版诊断/检查、操作人/时间/理由、用药原项与追加更正引用、正式采纳内容/责任，标识误建及无正文的关联编号。未采纳和待决建议不混入正式病历。原模板字段含义按记录引用模板解释，不以当前生效模板替换。导出登记保存用途和范围，管理审计只见脱敏操作元数据，不见用途或临床内容；按病历编号可查到这次导出事件。

## 医生命中明细：`POST /api/exports/alert-hits`

权限`export.guard_detail`。请求允许`scope`、`time_range`、`rule_ref`、`handling_state`。scope若提供只允许`{"encounter_refs":["UUID",...]}`；每个目标须在当前医生本店接诊范围内，否则整次403。状态为`unhandled|handled`。没有额外必填用途。

列顺序：`event_ref,record_ref,rule_ref,hit_field,handling_state,handler_ref,handled_at,handling_result`。每命中一行；阻断首次正式写入时可能尚无record_ref，此列为空。仅含规则/命中字段和已有处理信息，不含原始命中值、病历正文、接待资料或其他接诊。处理结果是医生获准的本条处理信息；管理汇总永不包含它。导出不改变告警处理状态；有病历关联的事件可按病历编号在脱敏审计中检索。

## 管理命中汇总：`POST /api/exports/alert-summary`

权限`export.guard_summary`。请求允许`time_range`、`rule_ref`、`handling_state`及`group_by=rule|handling_state`，默认rule。严禁病例范围、接诊/宠物编号和自由文本用途。仅本店聚合，状态取导出时已处理/未处理，消息投递重试不增加命中/告警。

列顺序：`time_range,rule_ref_filter,handling_state_filter,group_by,rule_ref,handling_state,hit_count,alert_count,unhandled_count`。筛选口径重复写入每个汇总行，按规则或按处理状态分组。无匹配结果仅有表头，`X-Record-Count: 0`。绝不包含病历/命中事件标识、宠物身份、临床值、建议原文或医生自由理由。无额外必填用途。

## 错误与前端反馈

以下是合成请求形状，UUID应由已获准的列表/详情取得，不代表本机真实数据：

```json
{"scope":{"record_refs":["11111111-1111-4111-8111-111111111111"]},"purpose":"课程演示病历核对"}
```

```json
{"scope":{"encounter_refs":["22222222-2222-4222-8222-222222222222"]},"rule_ref":"SR-02","handling_state":"unhandled"}
```

```json
{"group_by":"handling_state","time_range":{"from":"2026-10-23T00:00:00+08:00","to":"2026-10-24T00:00:00+08:00"}}
```

分别发往上列三个接口。成功响应体直接是CSV字节，不是JSON包装；失败例如 `{"ok":false,"error":{"code":"TARGET_UNAVAILABLE"}}`，不得当作下载文件。文件完整接收核验使用响应实际字节长度及 `X-Content-SHA256`，不能只看HTTP 200。文件名取 `Content-Disposition`；`X-Export-Ref`是本次导出留痕编号，不是可跳过权限的重下载凭据。

401未登录、403角色/范围拒绝或CSRF、405方法不符、422字段/引用/时间非法、503数据/审计/生成不可确认。失败响应不应被前端保存成`.csv`或显示导出成功。服务端响应头及文件字节由前端校验；浏览器若报告下载已开始，不代表已完整接收。Excel等表格软件对以公式符号开头的原始临床文本可能做公式解释；下载端应按不执行公式的文本/CSV方式处理，保持本CSV中原始字段可还原。

实现与测试在`../backend/clinic/exports.py`、`test_exports.py`和`../10_验收证据/18_受限完整CSV导出/`。本接口契约不声称正式280ms、360并发或2万病历规模已达标；正式测量按后续协议与原始结果如实报告。

### CSV传输压缩

导出端点支持协商gzip（请求Accept-Encoding包含gzip时由Django标准压缩中间件处理，含随机填充）。Content-Length为HTTP压缩表示的传输长度；X-Byte-Count、X-Content-SHA256、ExportRequest.byte_count始终描述解压后的完整UTF-8 CSV。未压缩时两种长度相同。浏览器完成解压后核对CSV字节数及SHA；发压工具额外记录实际wire_bytes并核对Content-Length。完整响应计时包含接收及自动解压。服务端仍先完成生成和必需审计提交，再提供响应。
