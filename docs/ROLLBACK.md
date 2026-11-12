# 回滚说明

## 1. 前置条件

- 只有批次状态为 `completed` 才能回滚；`failed`、`running`、
  `rollback_partial`、`rolled_back` 均拒绝再次回滚。
- 回滚使用台账中的 `source_rel` / `target_rel`，不接受用户传入任意路径。
- 回滚不读取或重算计划哈希，而是以批次台账为权威；原计划文件可以移动或
  删除，不影响回滚。

## 2. 流程

1. 读取批次及其 `moved` / `marked` 动作。
2. 先处理 `marked`（purge 标记）：只在台账中标记 `unmarked`，没有文件动作。
3. 其余动作按 `seq` 逆序处理。对每个动作：
   - 目标存在且原路径不存在：用临时名 + 原子 rename 移回原路径，动作状态
     记为 `rolled_back`，计入 `restored_count`。
   - 目标缺失、原路径已存在，或两者都存在：不覆盖任何路径，动作记为
     `unrecoverable`，写入明确原因。
   - 文件系统操作异常：同样记 `unrecoverable`，继续处理其他动作。
4. 写入 `rollbacks` 记录；无不可恢复项时批次为 `rolled_back`，否则为
   `rollback_partial`。

回滚成功后会保留目标侧空目录（如果有），但所有数据文件均回到原路径；不会
自动删除任何非空目录。

## 3. 失败模式与 `unrecoverable` 判定

| 场景 | 判定 |
| --- | --- |
| 移动目标被外部移走 | `unrecoverable: moved target is missing; path was changed externally` |
| 原路径被外部重建 | `unrecoverable: both original and moved paths exist; refusing to overwrite` |
| 恢复移动抛异常（权限、设备、磁盘） | `unrecoverable`，原因带异常类型与消息 |
| 部分动作不可恢复、其余成功 | 批次 `rollback_partial`，两个计数都写入回滚记录 |
| 全部可恢复 | 批次 `rolled_back`，`unrecoverable_count = 0` |

`unrecoverable` 是**明确结论**，不是静默跳过：CLI 输出、批次页、Markdown/JSON
报告和 `rollbacks.detail_json` 都会列出分区、源/目标路径与原因。

## 4. 命令与服务入口

```bash
python -m tier_triage rollback --batch <batch_id> \
  --lake data/lake --db data/ledger/tier_triage.sqlite \
  --result data/artifacts/rollback_result.json
```

也可在本地服务 `http://127.0.0.1:5000/batches/<batch_id>` 点击回滚。视图只做
参数收集和渲染，实际回滚调用与 CLI 相同的
`tier_triage.exec.rollback.rollback_batch` 服务层函数。

## 5. 审计口径

- 每个动作保留原始 `seq`、源/目标、字节、文件数和错误。
- 回滚记录包含 `restored_count`、`unrecoverable_count` 与完整 `detail`。
- 回滚只移动数据，不执行物理删除；`.trash` 软删除能力保留给未来显式审批
  流程，本版本不自动使用。
