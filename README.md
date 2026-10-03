# 矿井应急避险与通风协调

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8335`。领域对象包括矿井人员、气体传感、通风设备、逃生通道、避险硐室、事件和处置任务。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8335
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8335/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/offline-records`：合并现场离线记录（幂等）。
- `POST /api/offline-records/replay`：按现场时间批量重放进出记录并重算硐室占用。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于合并现场离线记录，`source_id + record_id` 相同会幂等返回原记录。

现场断网期间登记的进出记录，通过 `POST /api/offline-records/replay` 按现场时间（`recorded_at`）重放成一批入账，而不是按记录号逐条塞入。重放以完整事件日志为准重算硐室占用：同一人的进出顺序自相矛盾（未进先出、重复进入等）会整批停下并列出冲突记录（状态置为 `conflict`）；重算后占用超过硐室容量会拒绝整批并回滚，不写入任何占用。写入失败时未处理的记录保留 `pending` 状态，可直接重试。管理员可用 `resolve` 动作核对标记为 `conflict` 的记录。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`。
- 通风设备复转前，必须确认所在区域没有报警气体、没有未撤出的人员。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务，所有通风设备恢复运行，硐室占用不超过容量，且没有未核对的进出冲突。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
