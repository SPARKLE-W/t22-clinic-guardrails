# 门诊护栏 · 前端工作台

同源单页工作台，入口 `http://127.0.0.1:18730/app`。原生 HTML/CSS/JS，无构建、无第三方运行时依赖；由 Django 在同一来源提供页面和 `/api/`，携带会话 Cookie 与 CSRF。

## 文件

| 文件 | 作用 |
| --- | --- |
| `index.html` | 壳、启动占位、样式与脚本引用 |
| `assets/styles.css` | 纸色/墨线/印章设计系统、组件、弹窗、响应式、动效 |
| `assets/app.js` | hash 路由、登录与会话、API 封装、三角色视图、表单、错误映射、幂等键、草稿保留 |

## 运行

在 `05_系统代码` 目录按主 [README](../README.md) 启动 MySQL 与 Django 后，打开 `/app`。首次打开会设置 `csrftoken`；`GET /api/session` 只返回当前账号最小身份。退出后回到登录页。

演示账号在仓库外的用户缓存目录 `app-demo-accounts.json`，不随代码提交。

## 角色与路由

- 前台：`#/receptions`、`#/receptions/new`、`#/receptions/{id}`
- 医生：`#/clinical`、`#/clinical/{id}`、`#/alerts`
- 管理：`#/governance`、`#/governance/{id}`、`#/audit`

前端路由只做角色归位，不代替服务端鉴权。

## 约定

- 写请求使用 `Idempotency-Key`；同一路径 + 同一 JSON 内容复用同一键，成功或明确 `GUARD_BLOCKED` 后放弃，网络/503 保留以便重试核对。
- 409 显示版本冲突并保留输入；422 `GUARD_BLOCKED` 在弹窗显示 `event_ref` 与「病历未改变」；401 提示重新登录。
- 表单输入与弹窗草稿写入 `sessionStorage`（仅当前标签页），提交成功后清除。
- 所有服务端字符串经 `esc()` 转义后插入 DOM。

## 设计

方向、配色、字体、页面结构与未验证项见[15前端界面设计](../../docs/04_%E8%AE%BE%E8%AE%A1%E4%B8%8E%E6%8E%A7%E5%88%B6%E6%96%87%E4%BB%B6/15_%E5%89%8D%E7%AB%AF%E7%95%8C%E9%9D%A2%E8%AE%BE%E8%AE%A1.md)。截图与录屏在[第十四阶段](../../docs/10_%E9%AA%8C%E6%94%B6%E8%AF%81%E6%8D%AE/14_%E5%89%8D%E7%AB%AF%E7%95%8C%E9%9D%A2%E4%B8%8E%E6%B5%8F%E8%A7%88%E5%99%A8%E9%AA%8C%E8%AF%81/02_%E6%9C%AC%E6%89%B9%E7%BB%93%E6%9E%9C.md)。
