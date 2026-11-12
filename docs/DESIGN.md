# 设计说明

## 1. 数据模型

| 实体 | 关键字段 | 说明 |
| --- | --- | --- |
| 分区 Partition | `partition_id`、`layer`、`partition_parts`、`file_count`、`total_bytes`、`last_modified`、`largest_file` | 从 `<lake>/<tier>/<db>/<table>/key=value/...` 扫描得到；同一 `db.table.spec` 跨层重复时第二个进入 `unparsed` |
| 访问统计 Usage | `reads_Nd`、`bytes_Nd`、`last_access`、`idle_days`、`never_accessed` | 仅统计 `read/scan/select`，且时间戳不晚于评估时钟 |
| 快照 Snapshot | `snapshot_id`、`inventory_hash`、`inventory_json` | 扫描结果的稳定摘要；`snapshot_id = snap-<hash 前 16 位>` |
| 策略 Policy | `fingerprint`、`defaults`、`tables`、`windows` | 严格 schema 校验；指纹是规范化 YAML 数据的 SHA-256 |
| 判定 Decision | `tier`、`score`、`checks`、`reasons`、`matched_rules` | 四档结论及完整理由链 |
| 计划 Plan | `plan_id`、`plan_hash`、`actions[]`、`summary` | 动作类型 `move / mark_purge / noop`，状态 `ready / blocked / noop` |
| 批次 Batch | `batch_id`、`plan_id`、`status`、`moved_count`、`marked_count` | 状态包括 `running / completed / failed / rolled_back / rollback_partial` |
| 动作 Action | `seq`、`source_rel`、`target_rel`、`bytes`、`status` | 台账中保留初始状态、执行结果和回滚结果 |
| 回滚 Rollback | `batch_id`、`restored_count`、`unrecoverable_count`、`detail_json` | 每次回滚一条记录 |

SQLite 表：`snapshots`、`policies`、`plans`、`batches`、`actions`、
`rollbacks`。动作表对 `(batch_id, seq)` 唯一。

## 2. 确定性设计

**时钟注入。** 所有年龄、窗口、最后访问判断和 `as_of` 输出都来自
`--as-of`；计划不读取系统墙钟。批次与台账的执行时间可以不同，因为它们不
进入计划哈希。

**稳定排序。**

1. 清单分区按 `(table_name, partition_spec, layer)` 排序，JSON 以
   `sort_keys=True` 序列化。
2. 判定按 `partition_id` 排序。
3. 动作先按 `move < mark_purge < noop`、状态 `ready < blocked < noop`、
   再按表/分区/ID 排序，最后赋 `seq`。
4. 策略命中规则先按 `(priority, match)` 升序应用字段覆盖，输出命中链按时
   优先级降序。
5. 访问记录按 `(ts, line)` 排序，保证相同日志输入得到相同的最后访问时间。

**哈希口径。**

- `inventory_hash = sha256(canonical({schema_version, layout, partitions,
  tables, unparsed}))`，不包含湖根绝对路径和扫描时间。
- `policy fingerprint = sha256(canonical(加载后的策略数据))`。
- `plan_hash = sha256(canonical(plan 去掉 plan_hash/plan_id))`。因此修改任意
  动作字段而不重算哈希，apply 会在预检阶段拒绝。
- `plan_id = plan-<plan_hash 前 16 位>`。

**事务边界。** 批次执行前完成全部前置校验；每个动作先以
`.tier-triage-tmp-*` 临时名移动，再原子 rename 到目标，并逐步写台账。任一步
失败时逆序还原已移动项、清理本批次创建的空目录，批次与动作均留下失败记录。

## 3. 分层不变量

- `hot/warm/cold` 三层始终位于同一湖根之下；目标路径由动作源路径替换层级
  前缀得到，禁止跨设备移动。
- 目标已存在、源不存在、路径逃逸或磁盘余量不足时动作标记 `blocked` 或整批
  预检失败。
- `purge_candidate` 只生成 `mark_purge`，不转移、不删除文件。
- 回滚只接受 `completed` 批次，绝不覆盖已存在的原路径或目标路径。
