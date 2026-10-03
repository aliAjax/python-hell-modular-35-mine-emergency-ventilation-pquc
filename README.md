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
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于回传现场离线记录，`source_id + record_id` 相同会幂等跳过。

### 离线回传：按现场时间重放成一批

回传的记录先以`pending`原样暂存（写库失败也不丢，可拿同一批继续重试），随后按记录自身的`recorded_at`（`seq`用于同刻排序）整批重放，最后在**一个数据库事务**里入账：

- 硐室进出记录：`payload`形如`{"type":"refuge_movement","refuge_id":"...","worker_id":"...","direction":"enter|exit"}`。占用始终按所有已入账移动记录以现场时间顺序重算，不依赖记录号顺序。
- 同一人的进出顺序自相矛盾（重复进入、未进先出、未从A硐室撤出又进入B硐室）时**整批停下**，冲突记录标记为`conflict`并写入冲突清单（HTTP 409，响应含`conflicts`），其余记录保持`pending`。经`review_conflict`动作人工核对后可重发剩余记录继续重试。
- 通用状态动作：`payload`形如`{"type":"entity_action","kind":"...","entity_id":"...","action":"...","data":{...},"base_version":N}`。中心版本已更新时，对动作在**当前状态上重新校验**（重算）而不是照单覆盖；当前状态不再接受的旧动作记为`stale_action`冲突。
- 重放完成后按最新进出核对每间避险硐室占用，**超过容量则拒绝整批并回滚**，全部记录留在`pending`。
- 其他`payload`（如气体读数）按信息记录入账，不改变实体状态。
- `GET /api/refuge-occupancy`可随时查看按重算得到的各硐室占用。

### 风机复转与事件收尾

- 风机`restore`前，所在区域不得有报警气体（传感器状态或`severity`为`alarm`），且不得有未撤出人员（同区域处于`active/missing/located`的人员）。
- 事件`close`前除原有条件外，还要求：重算后的避险硐室占用全部为零、所有风机运行中、没有未核对（未经`review_conflict`）的离线冲突。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`；传感器`clear`时同步重算为`normal`。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务、所有通风设备恢复运行、硐室无人且没有未核对的回传冲突。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
