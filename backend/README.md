# T22 后端运行说明

本文件说明后端组成、配置、启动、队列运维和离线编译。课程资料、需求、设计与验收证据统一在 [`docs/`](../docs/00_作业导航.md)。系统由 Django/DRF、MySQL、RabbitMQ 与同源 Web 工作台组成；前端源码在 [`frontend/`](../frontend/README.md)。

当前性能结论以[测试报告](../docs/06_测试与实测报告.md)及[最新问题和测试总结](../docs/10_验收证据/19_后端综合验收/32_当前问题与测试总结.md)为准；本文件作为运行手册，不保留易过期的阶段状态。

前端工作台入口为 `/app`，使用原生 HTML/CSS/JS，无构建步骤。界面方向、路由、错误/幂等约定和未验证项见[15前端界面设计](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/15_%E5%89%8D%E7%AB%AF%E7%95%8C%E9%9D%A2%E8%AE%BE%E8%AE%A1.md)；浏览器截图与操作录屏见[第十四阶段](../docs/10_%E9%AA%8C%E6%94%B6%E8%AF%81%E6%8D%AE/14_%E5%89%8D%E7%AB%AF%E7%95%8C%E9%9D%A2%E4%B8%8E%E6%B5%8F%E8%A7%88%E5%99%A8%E9%AA%8C%E8%AF%81/02_%E6%9C%AC%E6%89%B9%E7%BB%93%E6%9E%9C.md)。原有模板保留作联调入口，不再扩展样式。

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

部署顺序：在联调停服窗口停止既有Web/队列工作进程，先备份并检查待应用迁移，再执行 `python manage.py migrate --noinput`，之后启动对应新代码；旧进程与新表结构不能混跑。历史共享合成应用库升级与冒烟记录见[验收证据](../docs/10_验收证据/19_后端综合验收/03_共享应用升级与联调.md)。其他机器仍须独立备份、迁移和验证。

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

published只是取得发布确认；持久消费收据存在后才是delivered。消息仅event_ref/alert_ref及AMQP message_id，不含临床正文；重复消费不新增告警，不替医生处理。具体协议、运行状态、异常和机器契约见[16队列设计](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/16_%E5%91%8A%E8%AD%A6%E9%98%9F%E5%88%97%E6%8A%95%E9%80%92%E4%B8%8E%E6%81%A2%E5%A4%8D.md)及[第十五阶段](../docs/10_%E9%AA%8C%E6%94%B6%E8%AF%81%E6%8D%AE/15_%E5%91%8A%E8%AD%A6%E9%98%9F%E5%88%97%E6%8A%95%E9%80%92%E4%B8%8E%E6%81%A2%E5%A4%8D/02_%E6%9C%AC%E6%89%B9%E7%BB%93%E6%9E%9C.md)。

完整含MQ的回归须启动RabbitMQ并设置T22_MQ_CONFIG；不提供配置时10项真实队列测试会明确跳过，不能当作完整通过。CI已增加RabbitMQ服务和临时配置，含真实队列回归。为不干扰另一批工作，本机证据运行器仅将TEST.NAME改为t22_app_mq_test（账号需此独立库权限），正常产品配置不改，也不flush应用库或另一批测试库。

## 演示建议接口

迁移0005增加建议与一对一终态决定。对一个已创建的合成接诊执行 `python manage.py seed_demo_suggestions --encounter-ref UUID`，保存两条明确标注的演示建议，重复执行不重复准备、不修改原文、不写病历。不要用真实临床数据演示，自动化采纳不表示真人医生认可。

负责医生调用GET `/api/clinical/encounters/{UUID}/suggestions`读取；POST `/api/clinical/suggestions/{UUID}/adoption` 或 `/decline` 作决定。来源不可自报；采纳通过当前护栏、原模板、旧版本核对及同事务责任登记，不采纳不写临床。请求仍要求真实会话/CSRF，业务变更带UUID幂等键。

具体字段、版本及错误见[14接口契约](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/14_%E6%BC%94%E7%A4%BA%E5%BB%BA%E8%AE%AE%E4%B8%8E%E4%BA%BA%E5%B7%A5%E5%86%B3%E5%AE%9A%E6%8E%A5%E5%8F%A3.md)；工具可导入[三个建议端点OpenAPI](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/03_%E5%BB%BA%E8%AE%AE%E6%8E%A5%E5%8F%A3_openapi_v0.1.json)。[第十三阶段](../docs/10_%E9%AA%8C%E6%94%B6%E8%AF%81%E6%8D%AE/13_%E6%BC%94%E7%A4%BA%E5%BB%BA%E8%AE%AE%E4%B8%8E%E4%BA%BA%E5%B7%A5%E5%86%B3%E5%AE%9A/03_%E6%9C%AC%E6%89%B9%E7%BB%93%E6%9E%9C.md)保留18项专项、107项完整MySQL回归、21项实际HTTP核对和一次定向工程复核；该批没有增加页面，建议操作与桌面兼容已在新工作台接入后另测（第十四阶段）。

## 治理应用

Python3.12环境按requirements.lock安装；创建独立`t22_app`数据库、仅访问`t22_app`及测试库`t22_app_test`的MySQL账号。不要复用已有预研业务表。口令、Django密钥和运行数据放代码外。设置`T22_APP_CONFIG`指向私密JSON配置，结构如下（值替换为本机实际配置，不使用示例占位作为密码）：

```json
{"secret_key":"随机生成的密钥","database":{"HOST":"127.0.0.1","PORT":13322,"NAME":"t22_app","USER":"t22_app","PASSWORD":"本机数据库口令"}}
```

`database`还可选设`CONN_MAX_AGE`（秒，默认0，按请求关闭连接）及`CONN_HEALTH_CHECKS`（布尔值，默认false）。启用持久连接时，每个Web工作线程可能保留一个数据库连接；应据实际工作线程数预留MySQL连接额度，并建议同时开启连接健康检查。

在本目录执行：

```powershell
$env:T22_APP_CONFIG = 'C:/你的私密目录/app.json'
python manage.py migrate --noinput
python manage.py seed_demo --credentials-file C:/你的私密目录/demo-accounts.json
python manage.py runserver 127.0.0.1:18730 --noreload
```

seed_demo只用于空应用库，生成两店/六个合成账号；重复执行会拒绝，不重置口令或覆盖数据。凭据文件必须新建且在代码目录外，需自行保管，不随提交包分享。如需连接已有本机演示库，请使用仓库外由操作者保管的凭据文件；本仓库不承诺其他机器上的数据库或账号状态。新工作台入口为`http://127.0.0.1:18730/app`，旧联调页仍在`http://127.0.0.1:18730/`；使用实际凭据登录；管理可载入合成定义或导入文件，填写理由后依次生成、提交、批准/驳回、手动启用。每步独立，批准不会自动启用。

已有本地开发环境可用`运行脚本/01_本地应用.ps1 start|stop`，该脚本只管理自己启动的回环Web进程，数据库单独管理；日志在用户缓存，不装Windows服务。Django runserver用于课程本地演示，不把它当已达标生产部署。手工终端前台运行时用Ctrl+C退出。

完整回归使用`python manage.py test tests governance clinic --noinput -v 2`，只创建/删除隔离的`t22_app_test`，不改`t22_app`或预研库；不要把个人数据放测试库。接口、字段映射、锁顺序、状态和范围见[09运行契约](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/09_%E6%B2%BB%E7%90%86%E8%BF%90%E8%A1%8C%E6%8E%A5%E5%85%A5%E4%B8%8E%E8%B4%A6%E5%8F%B7%E5%9F%BA%E7%A1%80.md)及[10接诊读取说明](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/10_%E6%8E%A5%E8%AF%8A%E5%BD%92%E5%B1%9E%E4%B8%8E%E8%AF%BB%E5%8F%96%E6%9D%83%E9%99%90%E5%AE%9E%E7%8E%B0.md)。演示批准/启用来自AI自动化功能验证，非真人认可，未用于真实临床。

已有环境更新代码后先执行migrate。前台可建档、更正、标误建；先由本店管理启用模板才可创建。医生可读取负责接诊、填写诊断/可选检查并追加更正（理由必填），误建单仅允许已有内容纠错。缺诊断实际提交会被护栏阻断并保存告警，医生工作台可定位对应接诊和登记处理结果。新宠物编号为UUID，已知编号核验仅在本店，不返回历史。当前证据见10目录第11阶段；旧证据保持原位置。应用库现有内容均为明确的合成HTTP/浏览器演示，不是真实诊疗。

运行实现见[11病历与护栏说明](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/11_%E7%97%85%E5%8E%86%E5%86%99%E5%85%A5%E4%B8%8E%E8%BF%90%E8%A1%8C%E6%8A%A4%E6%A0%8F%E5%AE%9E%E7%8E%B0.md)及[12用药追加与更正](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/12_%E5%A4%84%E6%96%B9%E7%94%A8%E8%8D%AF%E8%BF%BD%E5%8A%A0%E4%B8%8E%E6%9B%B4%E6%AD%A3%E5%AE%9E%E7%8E%B0.md)。医生打开病历后可追加用药或选择已有项进行关联更正，原项保留，写入响应不内嵌正文。可选`x_`临床字段在迁移0009后由后端按原模板校验并保留各版；当前前端工作台未提供动态扩展字段编辑。升级现有环境须先备份、迁移并重启对应代码。完整回归不是正式性能验收。

## 受限CSV导出

`POST /api/exports/clinical-records`（医生负责的本店病历，范围及用途必填）、`POST /api/exports/alert-hits`（同范围医生命中明细）及`POST /api/exports/alert-summary`（管理本店脱敏汇总）均已接入。按当前账号重新授权，完整生成UTF-8 CSV并写必需审计后才提供字节；明示文件长度、SHA-256和导出引用。服务端成功只表示已就绪，接收端须取得完整文件并校验。示例、字段、拒绝状态与机器契约见[20接口](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/20_%E5%8F%97%E9%99%90%E5%AE%8C%E6%95%B4CSV%E5%AF%BC%E5%87%BA%E6%8E%A5%E5%8F%A3.md)及[阶段18证据](../docs/10_%E9%AA%8C%E6%94%B6%E8%AF%81%E6%8D%AE/18_%E5%8F%97%E9%99%90%E5%AE%8C%E6%95%B4CSV%E5%AF%BC%E5%87%BA/02_%E6%9C%AC%E6%89%B9%E7%BB%93%E6%9E%9C.md)；共享服务迁移与前端完整接收证据分别见第19和第14阶段。

## 离线编译

静态控制包编译与治理服务分开运行。管理汇总字段见[17汇总接口](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/17_%E7%AE%A1%E7%90%86%E8%84%B1%E6%95%8F%E6%B1%87%E6%80%BB%E6%8E%A5%E5%8F%A3.md)，人机分工与导出见19、20号接口。完整回归和环境升级证据按各验收批次归档；阅读时以对应源码提交和报告记录为准。

运行环境为Python 3.12，依赖入口requirements.in，实际验证版本锁定在requirements.lock。建议虚拟环境放用户缓存或项目外的ASCII路径，源代码和证据不随环境混放。使用常规Python安装工具从requirements.lock安装即可；锁文件不是平台无关的二进制镜像。

在本目录执行（以下python指向已安装锁定依赖的环境）：

```powershell
python -m unittest discover -s tests -v
python -m vetcontrols compile --input examples/01_完整定义需求_v0.2.json --output C:/Temp/t22-controls-new
python -m vetcontrols verify --candidate C:/Temp/t22-controls-new/candidate.json --output C:/Temp/t22-controls-verify-new
```

输出目录必须不存在，父目录必须已存在且可写，示例中C:/Temp需按机器实际路径替换。退出0表示静态检查通过，退出2表示拒绝/输入输出失败。成功compile输出候选和报告；失败不输出candidate.json。verify仅报告。既有结果目录被拒绝，不覆盖历史证据。

离线API为`compile_demand(dict)`、`compile_text(str|bytes)`和`verify_candidate(dict)`，统一返回ok/candidate/issues。离线产物始终未提交、未审批、未启用；不接受请求方指定真实账号或伪造审批。不可削弱规则、权限白名单、类型及资源边界见[机器契约](../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/08_%E6%8E%A7%E5%88%B6%E5%8C%85%E6%9C%BA%E5%99%A8%E5%A5%91%E7%BA%A6%E4%B8%8E%E9%9D%99%E6%80%81%E7%BC%96%E8%AF%91.md)。

`examples/01_完整定义需求_v0.2.json`是明确写出的定义输入，不是执行结果。`vetcontrols/data`中的底线、目录及schema为受信任发行资产；输入方不能替换。schema/目录变化需要代码评审与回归。静态包生成和运行治理状态分别保存；治理及诊断路径校验已接入，10个注册检查器的其他运行路径仍待后续，不把不适用标成已验证完成。

测试/CLI证据统一归[第七阶段](../docs/10_%E9%AA%8C%E6%94%B6%E8%AF%81%E6%8D%AE/07_%E6%8E%A7%E5%88%B6%E5%8C%85%E7%BC%96%E8%AF%91%E4%B8%8E%E5%AE%89%E5%85%A8%E6%A0%A1%E9%AA%8C/01_%E6%89%A7%E8%A1%8C%E8%AE%A1%E5%88%92%E4%B8%8E%E8%8C%83%E5%9B%B4.md)，初始失败也保留。此前B方案性能预研证据保留原位置，280ms正式指标仍未验收。本子批次不以测试全通过冒称课程要求全通过，不为凑有效代码行数增加代码。
