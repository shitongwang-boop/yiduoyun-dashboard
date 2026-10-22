# 一朵云项目进度看板

看板从企业微信智能表格「业务需求收集表」读取数据，支持服务器每小时整点自动刷新，以及右上角「数据刷新 → 一键刷新」。所有时间采用北京时间。浏览器每 30 秒检查服务端状态，刷新过程中每 2 秒检查；更新完成后重绘看板并保留日期和优先级筛选。

正式入口：https://techhub.zhaogangrentest.com/pm/yiduoyun-dashboard/

## 本地运行

需要 Python 3.9+、已授权的 `wecom-cli` 1.3.4，以及系统时区数据库。使用现有企业微信授权，勿将凭证提交到 Git。

```sh
mkdir -p /tmp/yiduoyun-dashboard
DASHBOARD_DATA_DIR=/tmp/yiduoyun-dashboard \
TECHHUB_SQLITE_PATH=/tmp/yiduoyun-dashboard/app.sqlite3 \
PORT=8080 python3 runtime_server.py
```

打开 `http://127.0.0.1:8080/`。

```sh
python3 -m unittest discover -s tests -v
node --check data-refresh.js
```

## 服务端运行

使用 Dockerfile 和 `.techhub.json` 发布。`/data/app.sqlite3` 持久化最新成功快照与刷新状态；仅开放页面、前端脚本与指定 API，配置、源代码和数据库不通过 HTTP 提供。部署时必须将已有且获授权的企业微信服务凭证配置到持久化私有目录 `/data/wecom`，目录权限 0700、文件权限 0600；不要把凭证放入镜像、网页或 Git。Dockerfile 只安装指定版本的企业微信 CLI 二进制，不内置凭证。

API：

- `GET api/health`：服务健康状态。
- `GET api/dashboard`：看板最新成功快照。
- `GET api/refresh/status`：刷新状态、更新时间与下次刷新时间。
- `POST api/refresh`：手动刷新；已有任务执行时加入同一次刷新，成功后 30 秒内复用结果。

自动和手动刷新共用单任务锁；数据必须通过字段及分页完整性检查，成功后才以 SQLite 事务保存。读取失败、空表或存储失败会保留上次成功快照。服务重启会恢复持久化数据，中断的任务会标记失败。刷新失败的详情不公开返回，界面支持重试。

旧的 GitHub Pages 入口通过首页脚本跳转到 TechHub，保留查询参数与锚点。迁移验收完成后暂停「一朵云驾驶舱自动刷新发布」本机定时任务，后续数据采集直接由服务器执行。
