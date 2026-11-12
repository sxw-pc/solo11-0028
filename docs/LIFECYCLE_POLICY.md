# 生命周期策略语义

## 1. 匹配与覆盖

策略文件包含 `version`、`windows`、`defaults`、`tables`。表规则用
`fnmatchcase` 匹配 `db.table`：

- `sales.orders` 精确匹配；
- `marketing.*` 匹配营销域全部表；
- `*` 匹配所有未被子规则覆盖的字段。

每个匹配规则按 `(priority, match)` 升序应用；`priority` 高者最后覆盖，因此
胜出。最终配置逐字段给出 `provenance`（来自 `defaults` 或某个 match），并在
判定的 `matched_rules` 中保留命中链。

规则只覆盖出现的字段，例如：

```yaml
tables:
  - match: "marketing.*"
    priority: 20
    purge_allowed: true
    purge_after_days: 120
```

## 2. 判定顺序

判定严格按以下顺序执行，前一步成立即确定层级；完整检查记录在 `checks`，
决定性理由记录在 `reasons`：

1. **热层保留期**：`age < hot_retain_days` ⇒ `hot`。这是强制下限，不再评估
   purge/cold/warm。
2. **purge 候选**：`purge_allowed` 且 `age >= purge_after_days` 且
   `reads_30d <= purge_max_reads_30d` 且（从未访问或
   `idle_days >= purge_idle_days`）⇒ `purge_candidate`。
3. **冷层**：`age >= cold_after_days` 且
   `reads_30d <= cold_max_reads_30d` ⇒ `cold`。
4. **温层**：`age >= warm_after_days`，或
   `reads_30d < hot_min_reads_30d`，或 `total_bytes > max_hot_bytes` ⇒
   `warm`。
5. 否则 ⇒ `hot`。

年龄优先取分区键日期（`dt/date/day/event_date` 的 `YYYY-MM-DD`）；无日期时
回退到文件最后修改时间。所有年龄以注入的 `--as-of` 秒级时钟计算，未来时间
截断为 0 天。`reads_30d` 只计读操作且窗口为左闭右闭
`[as_of-30d, as_of]`。

## 3. 计分

`score` 只是排序/观察辅助，不改变上述规则。它由 30 天读次数、空闲天数和
30 天读字节合成，范围 0–100，截断为整数，不含随机或当前时间因素。

## 4. 边界与约束

- `windows` 必须严格递增、全为正整数且包含 `30`，否则策略加载失败。
- `defaults` 的 10 个字段必须齐全；未知字段、错误类型、负值均失败，错误
  消息带 YAML 行号。
- 层次顺序必须满足：
  `hot_retain_days <= warm_after_days <= cold_after_days <= purge_after_days`；
  读取阈值满足：
  `purge_max_reads_30d <= cold_max_reads_30d <= hot_min_reads_30d`。
- 无匹配规则的表现在只用 defaults；不把空表规则视为错误。
- 同一表多个规则可分别覆盖不同字段；不建议让高优先级规则只覆盖部分相关
  阈值而不覆盖配套阈值。

## 5. 示例策略

`config/policy.yaml` 中：

- `sales.orders`：10 天热保底、35/120 天温冷阈值、禁止 purge。
- `marketing.*`：3 天热保底、20/60 天温冷、120 天后允许 purge 候选。
- `ops.audit_log`：长保留，180 天冷层，禁止 purge。
- `finance.*`：最高优先级严格保留，365 天冷层，禁止 purge。

`config/policy_strict.yaml` 抬高所有年龄阈值并关闭 purge，用于变更前复核或
敏感环境演练。
