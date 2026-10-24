# T22系统代码：控制编译、治理接入、队列恢复与前端工作台

> 2026-10-24当前状态：包含控制编译、三角色治理/接诊/临床、护栏与告警、演示建议、RabbitMQ投递恢复、管理汇总、人机分工、三类受限CSV导出及同源前端工作台。新增可选临床模板扩展字段后，独立MySQL/RabbitMQ完整Django回归186项通过；共享合成应用库已备份并升级到迁移0006—0008，新增0009仅在隔离合成库完成升级验证，共享18730尚未升级。正式负载两轮P95均未达280毫秒，第二轮实测见第19阶段05；前端F-06按本人接受的等效视口证据收口，浏览器菜单200%未验证。离线编译无需服务；应用使用独立MySQL，队列工作进程另需RabbitMQ与外部配置。

前端（2026-10-23）：工作台已在 `frontend/` 完成，同源入口 `/app`，原生 HTML/CSS/JS，无构建步骤。界面方向、路由、错误/幂等约定和未验证项见[15前端界面设计](../04_设计与控制文件/15_前端界面设计.md)；浏览器截图与操作录屏见[第十四阶段](../10_验收证据/14_前端界面与浏览器验证/02_本批结果.md)。原有模板保留作联调入口，不再扩展样式。

## 前端工作台

启动服务后打开 `http://127.0.0.1:18730/app`，使用门店演示账号登录。工作台按角色显示前台、医生、管理入口；页面通过 `GET /api/session` 恢复最小身份，首次打开 `/app` 时初始化 CSRF Cookie。

文件组织：

```text
frontend/
├── index.html              壳与启动占位
└── assets/
    ├── styles.css          纸色、墨线、印章与响应式
    └── app.js              路由、接口、表单、弹窗、错误映射
```

后端为同源接入补的最小内容：`GET /app`、`GET /app/`、`GET /app/assets/<path>`、`GET /api/session`；三者登记为公开入口，不返回临床内容。静态资源路径逃逸返回 404，会话接口只返回 `username`、`role`、`store_ref`、`store_name`。对应测试在 `governance/tests.py::test_frontend_shell_session_handoff_and_scoped_assets`。

前台建档/更正/版本冲突、医生首次诊断/更正/用药追加与关联更正/采纳建议/告警处理、管理生成候选/提交/批准/启用已在第十四阶段留有真实接口和浏览器证据。F-03/F-04、最终证据及验收记录PR #20/#21/#23/#25/#26已合并；F-05的Edge 2× device scale/683 CSS视口由本人接受为200%等效证据，浏览器菜单200%未验证。浏览器证据不替代正式性能或后端综合验收。

## 告警队列与恢复

部署顺序：在联调停服窗口停止既有Web/队列工作进程，备份后执行 `python manage.py migrate --noinput`（含0006—0008），再启动对应新代码；旧进程与新表结构不能混跑。本机共享合成应用库`t22_app`已于2026-10-23按此顺序完成0006—0008，18730从合并F-03/F-04的主分支重启并通过受限接口冒烟；具体备份摘要和结果见`../10_验收证据/19_后端综合验收/03_共享应用升级与联调.md`。其他机器仍须独立备份、迁移和验证。

Web护栏拒绝仍只做同事务告警/outbox持久化，不等待RabbitMQ网络发送。后台进程读取仓库外 `T22_MQ_CONFIG` JSON，键为host、port、virtual_host、user、password、queue；host仅本机回环，queue使用 `t22.app.` 前缀。口令使用本机真实配置，不放源码/消息/日志。可执行：

```powershell
$env:T22_MQ_CONFIG = 'C:/你的私密目录/app-mq.json'
python manage.py alert_delivery --once --batch-size 100
python manage.py alert_delivery --phase publish --once
python manage.py alert_delivery --phase consume --once
python manage.py alert_delivery --inspect 传输事件UUID
python manage.py alert_delivery --interval 5
```

不带once时持续轮询，Ctrl+C停止；不会安装系统服务。发布阶段由同机短生命周期子进程执行并受10秒等待上限约束，子进程不接数据库，超时仍保留同一事件供恢复。只读inspect不需要连接RabbitMQ，返回传输元数据；它是内部维护命令，不开放管理查看临床告警或公开重发API。

published只是取得发布确认；持久消费收据存在后才是delivered。消息仅event_ref/alert_ref及AMQP message_id，不含临床正文；重复消费不新增告警，不替医生处理。具体协议、运行状态、异常和机器契约见[16队列设计](../04_设计与控制文件/16_告警队列投递与恢复.md)及[第十五阶段](../10_验收证据/15_告警队列投递与恢复/02_本批结果.md)。

完整含MQ的回归须启动RabbitMQ并设置T22_MQ_CONFIG；不提供配置时10项真实队列测试会明确跳过，不能当作完整通过。CI已增加RabbitMQ服务和临时配置，含真实队列回归。为不干扰另一批工作，本机证据运行器仅将TEST.NAME改为t22_app_mq_test（账号需此独立库权限），正常产品配置不改，也不flush应用库或另一批测试库。

## 演示建议接口

迁移0005增加建议与一对一终态决定。对一个已创建的合成接诊执行 `python manage.py seed_demo_suggestions --encounter-ref UUID`，保存两条明确标注的演示建议，重复执行不重复准备、不修改原文、不写病历。不要用真实临床数据演示，自动化采纳不表示真人医生认可。

负责医生调用GET `/api/clinical/encounters/{UUID}/suggestions`读取；POST `/api/clinical/suggestions/{UUID}/adoption` 或 `/decline` 作决定。来源不可自报；采纳通过当前护栏、原模板、旧版本核对及同事务责任登记，不采纳不写临床。请求仍要求真实会话/CSRF，业务变更带UUID幂等键。

具体字段、版本及错误见[14接口契约](../04_设计与控制文件/14_演示建议与人工决定接口.md)；工具可导入[三个建议端点OpenAPI](../04_设计与控制文件/控制文件/03_建议接口_openapi_v0.1.json)。[第十三阶段](../10_验收证据/13_演示建议与人工决定/03_本批结果.md)保留18项专项、107项完整MySQL回归、21项实际HTTP核对和一次定向工程复核；该批没有增加页面，建议操作与桌面兼容已在新工作台接入后另测（第十四阶段）。

## 治理应用

Python3.12环境按requirements.lock安装；创建独立`t22_app`数据库、仅访问`t22_app`及测试库`t22_app_test`的MySQL账号。不要复用已有预研业务表。口令、Django密钥和运行数据放代码外。设置`T22_APP_CONFIG`指向私密JSON配置，结构如下（值替换为本机实际配置，不使用示例占位作为密码）：

```json
{"secret_key":"随机生成的密钥","database":{"HOST":"127.0.0.1","PORT":13322,"NAME":"t22_app","USER":"t22_app","PASSWORD":"本机数据库口令"}}
```

在本目录执行：

```powershell
$env:T22_APP_CONFIG = 'C:/你的私密目录/app.json'
python manage.py migrate --noinput
python manage.py seed_demo --credentials-file C:/你的私密目录/demo-accounts.json
python manage.py runserver 127.0.0.1:18730 --noreload
```

seed_demo只用于空应用库，生成两店/六个合成账号；重复执行会拒绝，不重置口令或覆盖数据。凭据文件必须新建且在代码目录外，需自行保管，不随提交包分享。本机已初始化，现有凭据为用户缓存private目录下app-demo-accounts.json，不需再次seed。新工作台入口为`http://127.0.0.1:18730/app`，旧联调页仍在`http://127.0.0.1:18730/`；使用实际凭据登录；管理可载入合成定义或导入文件，填写理由后依次生成、提交、批准/驳回、手动启用。每步独立，批准不会自动启用。

本机已有环境还可用`运行脚本/01_本地应用.ps1 start|stop`，该脚本只管理自己启动的回环Web进程，数据库单独管理；日志在用户缓存，不装Windows服务。Django runserver用于课程本地演示，不把它当已达标生产部署。手工终端前台运行时用Ctrl+C退出。

完整回归使用`python manage.py test tests governance clinic --noinput -v 2`，只创建/删除隔离的`t22_app_test`，不改`t22_app`或预研库；不要把个人数据放测试库。接口、字段映射、锁顺序、状态和范围见[09运行契约](../04_设计与控制文件/09_治理运行接入与账号基础.md)及[10接诊读取说明](../04_设计与控制文件/10_接诊归属与读取权限实现.md)。演示批准/启用来自AI自动化功能验证，非真人认可，未用于真实临床。

已有环境更新代码后先执行migrate。前台可建档、更正、标误建；先由本店管理启用模板才可创建。医生可读取负责接诊、填写诊断/可选检查并追加更正（理由必填），误建单仅允许已有内容纠错。缺诊断实际提交会被护栏阻断并保存告警，医生工作台可定位对应接诊和登记处理结果。新宠物编号为UUID，已知编号核验仅在本店，不返回历史。当前证据见10目录第11阶段；旧证据保持原位置。应用库现有内容均为明确的合成HTTP/浏览器演示，不是真实诊疗。

运行实现见[11病历与护栏说明](../04_设计与控制文件/11_病历写入与运行护栏实现.md)及[12用药追加与更正](../04_设计与控制文件/12_处方用药追加与更正实现.md)。医生打开病历后可追加用药或选择已有项进行关联更正，原项保留，写入响应不内嵌正文。可选`x_`临床字段在迁移0009后由后端按原模板校验并保留各版；当前前端工作台未提供动态扩展字段编辑。升级现有环境须先备份、迁移并重启对应代码。完整回归不是正式性能验收。

## 受限CSV导出

`POST /api/exports/clinical-records`（医生负责的本店病历，范围及用途必填）、`POST /api/exports/alert-hits`（同范围医生命中明细）及`POST /api/exports/alert-summary`（管理本店脱敏汇总）均已接入。按当前账号重新授权，完整生成UTF-8 CSV并写必需审计后才提供字节；明示文件长度、SHA-256和导出引用。服务端成功只表示已就绪，接收端须取得完整文件并校验。示例、字段、拒绝状态与机器契约见[20接口](../04_设计与控制文件/20_受限完整CSV导出接口.md)及[阶段18证据](../10_验收证据/18_受限完整CSV导出/02_本批结果.md)；共享服务迁移与前端完整接收证据分别见第19和第14阶段。

## 离线编译

2026-10-23当前增量：演示建议、MQ投递恢复、管理汇总、人机分工及三类受限导出后端已在代码中接入；管理汇总字段见[17汇总接口](../04_设计与控制文件/17_管理脱敏汇总接口.md)，人机分工与导出见19、20号接口。最新完整回归及在用服务升级状态见本页开头和第18阶段证据；不能把旧批次的143项结果当作最新源或在用Web已升级的证明。

运行环境为Python 3.12，依赖入口requirements.in，实际验证版本锁定在requirements.lock。建议虚拟环境放用户缓存或项目外的ASCII路径，源代码和证据不随环境混放。使用常规Python安装工具从requirements.lock安装即可；锁文件不是平台无关的二进制镜像。

在本目录执行（以下python指向已安装锁定依赖的环境）：

```powershell
python -m unittest discover -s tests -v
python -m vetcontrols compile --input examples/01_完整定义需求_v0.2.json --output C:/Temp/t22-controls-new
python -m vetcontrols verify --candidate C:/Temp/t22-controls-new/candidate.json --output C:/Temp/t22-controls-verify-new
```

输出目录必须不存在，父目录必须已存在且可写，示例中C:/Temp需按机器实际路径替换。退出0表示静态检查通过，退出2表示拒绝/输入输出失败。成功compile输出候选和报告；失败不输出candidate.json。verify仅报告。既有结果目录被拒绝，不覆盖历史证据。

离线API为`compile_demand(dict)`、`compile_text(str|bytes)`和`verify_candidate(dict)`，统一返回ok/candidate/issues。离线产物始终未提交、未审批、未启用；不接受请求方指定真实账号或伪造审批。不可削弱规则、权限白名单、类型及资源边界见[机器契约](../04_设计与控制文件/08_控制包机器契约与静态编译.md)。

`examples/01_完整定义需求_v0.2.json`是明确写出的定义输入，不是执行结果。`vetcontrols/data`中的底线、目录及schema为受信任发行资产；输入方不能替换。schema/目录变化需要代码评审与回归。静态包生成和运行治理状态分别保存；治理及诊断路径校验已接入，10个注册检查器的其他运行路径仍待后续，不把不适用标成已验证完成。

测试/CLI证据统一归[第七阶段](../10_验收证据/07_控制包编译与安全校验/01_执行计划与范围.md)，初始失败也保留。此前B方案性能预研证据保留原位置，280ms正式指标仍未验收。本子批次不以测试全通过冒称课程要求全通过，不为凑有效代码行数增加代码。
