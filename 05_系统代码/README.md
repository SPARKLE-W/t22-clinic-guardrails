# T22系统代码：控制编译与治理接入

> 2026-10-22。当前包含离线控制编译、三角色登录、管理治理及前台接诊/医生读取页面。正式诊疗写入尚未开放。离线编译无需服务；应用需要独立MySQL，当前切片无需RabbitMQ。

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

seed_demo只用于空应用库，生成两店/六个合成账号；重复执行会拒绝，不重置口令或覆盖数据。凭据文件必须新建且在代码目录外，需自行保管，不随提交包分享。本机已初始化，现有凭据为用户缓存private目录下app-demo-accounts.json，不需再次seed。页面入口为`http://127.0.0.1:18730/`，使用实际凭据登录；管理可载入合成定义或导入文件，填写理由后依次生成、提交、批准/驳回、手动启用。每步独立，批准不会自动启用。

本机已有环境还可用`运行脚本/01_本地应用.ps1 start|stop`，该脚本只管理自己启动的回环Web进程，数据库单独管理；日志在用户缓存，不装Windows服务。Django runserver用于课程本地演示，不把它当已达标生产部署。手工终端前台运行时用Ctrl+C退出。

完整回归使用`python manage.py test tests governance clinic --noinput -v 2`，只创建/删除隔离的`t22_app_test`，不改`t22_app`或预研库；不要把个人数据放测试库。接口、字段映射、锁顺序、状态和范围见[09运行契约](../04_设计与控制文件/09_治理运行接入与账号基础.md)及[10接诊读取说明](../04_设计与控制文件/10_接诊归属与读取权限实现.md)。演示批准/启用来自AI自动化功能验证，非真人认可，未用于真实临床。

已有环境再次更新代码后先执行migrate。前台登录后可建档、更正、标误建；先由本店管理启用合成模板才可创建。医生只读其负责的接诊，未形成正式病历时明确显示，不提供诊疗编辑。新编号为UUID，已知编号核验仅在本店，不返回历史。当前证据见10目录第09阶段；旧编译/治理证据保持原位置。临床正式病历夹具只存在测试，不作为应用初始化。

## 离线编译

运行环境为Python 3.12，依赖入口requirements.in，实际验证版本锁定在requirements.lock。建议虚拟环境放用户缓存或项目外的ASCII路径，源代码和证据不随环境混放。使用常规Python安装工具从requirements.lock安装即可；锁文件不是平台无关的二进制镜像。

在本目录执行（以下python指向已安装锁定依赖的环境）：

```powershell
python -m unittest discover -s tests -v
python -m vetcontrols compile --input examples/01_完整定义需求_v0.2.json --output C:/Temp/t22-controls-new
python -m vetcontrols verify --candidate C:/Temp/t22-controls-new/candidate.json --output C:/Temp/t22-controls-verify-new
```

输出目录必须不存在，父目录必须已存在且可写，示例中C:/Temp需按机器实际路径替换。退出0表示静态检查通过，退出2表示拒绝/输入输出失败。成功compile输出候选和报告；失败不输出candidate.json。verify仅报告。既有结果目录被拒绝，不覆盖历史证据。

API为`compile_demand(dict)`、`compile_text(str|bytes)`和`verify_candidate(dict)`，统一返回ok/candidate/issues。候选始终未提交、未审批、未启用；不接受请求方指定真实账号或伪造审批。不可削弱规则、权限白名单、类型及资源边界见[机器契约](../04_设计与控制文件/08_控制包机器契约与静态编译.md)。

`examples/01_完整定义需求_v0.2.json`是明确写出的定义输入，不是执行结果。`vetcontrols/data`中的底线、目录及schema为受信任发行资产；输入方不能替换。schema/目录变化需要代码评审与回归。静态包生成和运行治理状态分别保存；Django治理入口现已接入，10个临床运行检查器仍待后续。

测试/CLI证据统一归[第七阶段](../10_验收证据/07_控制包编译与安全校验/01_执行计划与范围.md)，初始失败也保留。此前B方案性能预研证据保留原位置，280ms正式指标仍未验收。本子批次不以测试全通过冒称课程要求全通过，不为凑有效代码行数增加代码。
