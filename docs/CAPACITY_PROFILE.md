# 分区体积画像与容量压力决策

本文说明独立的**分区体积画像（volume profile）**能力、它如何参与分层与动作
决策，以及容量紧张时“优先下沉老的大分区，还是优先清退又小又旧的分区”这一
取舍的明确规则。核心原则：**画像不是展示信息，它在越过容量阈值时可以改变
结论的落点；但年龄从不单独决定一切。**

## 1. 产物与边界

体积画像是一个独立产物（默认 `data/artifacts/volume_profile.json`），与湖
清单（inventory）解耦：

| 字段 | 含义 |
| --- | --- |
| `schema_version` / `layout` | 产物版本与湖布局 |
| `as_of` | 可选评估时钟；纯物理快照时为 `null` |
| `inventory_hash` | 对应湖清单的哈希，消费者据此校验同源 |
| `profile_hash` | 覆盖除自身外完整产物的 SHA-256，用于完整性校验 |
| `partitions[]` | 每个分区的 `total_bytes`、`file_count`、`avg_file_size`、`status` |
| `anomalies[]` | 符号链接、不可读目录、缺失文件、非法目录等异常痕迹 |
| `summary` | 分区计数、总字节/文件数、各状态计数、异常计数 |

分区状态 `status` 取值：

- `ok`：完整采集；
- `incomplete`：根目录可读，但内部个别文件/子目录读取失败，字节数为部分值；
- `unreadable`：分区根目录本身不可读，字节数记 0，同时必带一条
  `permission_denied` 异常。

只有 `status=ok` 的分区，其实测字节才被视为**可信**并参与容量硬限判定；
`incomplete`/`unreadable` 的真实体积未知，体积分档记为 `unknown`，不会因为
记了 0 字节就被当成安全小分区而通过 `max_hot_bytes` 检查——这类分区回退到
正常的年龄/读次数评估，并在 `capacity.size_trusted=false` 中显式标注。

### 1.1 安全与不中断约定

- **符号链接不计体积**：任何 `is_symlink()` 为真的条目都不跟随、不累计，
  改为在 `anomalies` 中以 `symlink_skipped` 记录。该规则覆盖层、库、表、
  分区各级目录与分区内的文件/子目录，因此外部目录无法通过符号链接把字节
  注入画像。
- **无法解析的目录不计体积**：非法 `key=value` 目录分别记
  `malformed_partition_directory` / `invalid_partition_directory`。
- **缺文件或权限失败不中断整次扫描**：文件级失败把分区降为 `incomplete`；
  分区根不可读则降为 `unreadable` 并保留异常；库/表级目录不可读时记录
  分区范围异常，其余表照常出结果。
- 画像自带一次独立的分区候选遍历，因此即使清单扫描因权限丢弃了某个分区，
  该分区仍会以 `unreadable` 行出现在画像里，而不是静默消失。

### 1.2 确定性约定

- 所有目录遍历先按条目名排序再处理；输出分区按 `partition_id` 排序，异常按
  `(path, kind, detail, partition_id)` 排序。
- 哈希只覆盖文件系统事实与注入时钟，不含湖根绝对路径、不含墙钟时间。
- 同一湖目录重复运行产出**字节完全一致**的画像（含 `profile_hash`）。

## 2. 消费方式：评估与计划均为可选接入

`evaluate` 与 `plan` 通过 `--profile <path>` 接入画像；**不传时保持原有
年龄/读次数行为，结论与产物结构与旧版本完全一致**（回归测试与基线代码
逐字节比对保证）。接入时先校验 `inventory_hash` 同源、哈希完整、分区集合
齐全，任一不满足直接报错，拒绝拿错画像做决策。

### 2.1 评估环节（scorer）

画像接入后：

1. 分区的 `total_bytes` / `file_count` 以画像实测值为准（`size_source =
   volume_profile`），不再回退清单估值。
2. 新增硬检查 `capacity_hot_limit`：当实测字节 **严格大于** 策略
   `max_hot_bytes` 时，`hot_forbidden = true`。
3. 该检查位于热层保留期（`retention_floor`）之后：一个仍在保留窗口内、但
   体积越限的年轻分区，会被**明确地从 hot 改判 warm**，决定性理由链同时
   保留 `retention_floor`（通过）与 `capacity_hot_limit`（不通过），结论
   可解释，而不是被静默温降。
4. 体积分档 `size_band`（相对该表族阈值）：`empty / small / near_limit /
   oversize`，写入每个判定的 `capacity` 块，仅供画像模式使用。

无画像时，原有的内联字节判断（`warm_threshold` 中的 `bytes>max_hot_bytes`）
原样保留，不新增任何输出键。

### 2.2 计划环节（planner）

- 体积越限被改判 warm 的分区，其 move 动作的 `reason` 带
  `size_forced=warm ... band=oversize`，并附结构化 `capacity` 注解，计划里
  能直接解释“这是体积原因”。
- 计划新增仅画像模式存在的 `capacity_profile` 块（含 `profile_hash`、
  越限分区列表、容量压力明细），旧计划不含该块，旧哈希口径不变。

## 3. 容量紧张时的取舍：缓解顺序（relief order）

当 warm 层空间吃紧时，系统**不接受按年龄一刀切**。策略可声明可选的
`max_warm_bytes`（每表族 warm 容量预算；缺省或 0 表示不启用该守卫）。
规划器先计入该表族已在 warm 层驻留的字节，再让入 warm 的 move 按下面的
**缓解顺序**逐个消费剩余预算：

1. **能释放空间的动作优先于只在层间搬字节的动作**：`mark_purge`（进入
   清退复核、可回收空间）排在普通 `move` 之前。
2. **同类动作里，体积大的优先**：一个又老又大的分区，一次下沉缓解的压力
   远大于一堆小分区；且冷老数据重读风险最低，搬迁最便宜。
3. **同体积再比年龄**：更老的先动。
4. **再比 30 天读次数，最后用稳定的 `partition_id` 兜底**，保证全序确定、
   可复现。

完整排序键：`(action_class, -total_bytes, -age_days, reads_30d,
partition_id)`，其中 `action_class` 让 purge（可回收空间）整体前置。

**因此“又小又旧”的分区永远排在每个队列的尾部**，不会被优先清扫：它们
持有成本低、会通过正常的年龄/purge 路径进入清退复核，而不是被当作释放
空间的廉价手段。反过来，“读得少但体积巨大”的分区不会因为年龄没到就赖在
hot——体积硬限先把它移出 hot；若 warm 预算放不下，它被显式 `blocked` 并
注明容量原因，而不是把 warm 层悄悄挤爆。

### 3.1 预算口径与溢出处理

每表族 warm 的起始占用按**预计留存**计算：先计入当前驻留 warm 的字节，
再把本批次要离开 warm 的分区（下沉 cold 或进入 purge 复核）作为
`departing_warm_bytes` 冲减；之后入 warm 的 move 才逐个消费剩余预算。
入 warm 的 move 若使某表族预计 warm 字节超过 `max_warm_bytes`：

- 该动作标记 `blocked`，`reason` 写明预算、预计占用与该分区字节，并说明
  是按缓解顺序延后；
- 后续更小的分区仍可正常进入 warm（预算是逐动作判定，不是整批失败）；
- `capacity_profile.pressure.table_budgets` 给出每个表族的驻留、离开、
  预计占用字节与是否在预算内，`capacity_blocked` 给出全部被容量阻断的
  动作。

`max_warm_bytes` 是**可选字段**：不写时策略指纹与旧文件完全一致（加载器
在计算指纹前剥离可选容量字段），计划也不产生任何容量阻断。

## 4. 命令

```bash
# 独立画像（--as-of 可选；省略则为纯物理快照）
python -m tier_triage profile --lake data/lake \
  --as-of 2026-11-01T00:00:00Z --out data/artifacts/volume_profile.json

# 扫描时顺带产出画像
python -m tier_triage scan --lake data/lake \
  --profile-out data/artifacts/volume_profile.json --out data/artifacts/inventory.json

# 接入画像的评估与计划（不传 --profile 即旧行为）
python -m tier_triage evaluate --lake data/lake --usage data/usage/access.jsonl \
  --policy config/policy.yaml --as-of 2026-11-01T00:00:00Z \
  --profile data/artifacts/volume_profile.json --out data/artifacts/evaluation.json
python -m tier_triage plan --lake data/lake --usage data/usage/access.jsonl \
  --policy config/policy.yaml --as-of 2026-11-01T00:00:00Z \
  --profile data/artifacts/volume_profile.json --out data/artifacts/plan.json
```

## 5. 验收口径

1. 同一 fixture 连跑两次，画像与计划文件的 SHA-256 完全一致。
2. 构造体积跨越 `max_hot_bytes` 的分区：画像模式下它不留在 hot，计划动作
   的理由与 `capacity` 注解能解释为体积原因。
3. 构造权限不可读目录：画像出现 `permission_denied` 异常与 `unreadable`
   分区行，扫描、评估、计划全程不中断。
4. 不带 `--profile` 的旧命令：与基线版本的评估/计划产物逐字节一致。
