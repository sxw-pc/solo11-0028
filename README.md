# 冷热分诊台（tier-triage）

面向小团队自建数仓 / 数据湖的**分层决策与迁移编排引擎**。它扫描本地
Hive 风格湖目录、聚合本地访问日志、套用生命周期策略，产出可解释的分区
分层结论、确定性迁移计划、事务化执行台账和可回滚批次。

## 定位与边界

**是：**
- 对本地 `hot/warm/cold` 分层目录中的分区做 `hot / warm / cold /
  purge_candidate` 四档判定。
- 生成 `move / mark_purge / noop` 计划，默认 dry-run。
- 对已确认计划执行同机文件系统内的原子移动，写入 SQLite 台账。
- 支持整批回滚；purge 只生成清退复核清单，不物理删除。

**不是：**
- 不是采集、RPA、脱敏、质量校验、去重、实体解析。
- 不是压缩转码、小文件合并、列式重写。
- 不是云对象存储生命周期 API 对接、分布式执行、调度平台。
- 不是 Web 图表看板，不做用户体系、权限或多租户。

## 快速开始

要求 Python 3.10+，依赖 `PyYAML`、`Flask`、`pytest`（均已本地安装）。
全程离线，不访问网络或云对象存储。

```bash
# 1) 一条命令生成 fixture 并跑完整流程（通常数秒）
make demo                  # 等效：python scripts/run_demo.py
# 2) 一条命令跑测试（应无 skip）
make test                  # 等效：python -m pytest -q

# 3) 一条命令启动本地只读服务
make serve                 # 打开 http://127.0.0.1:5000（仅回环地址）
# 等效：python -m tier_triage serve --lake data/lake --db data/ledger/tier_triage.sqlite
```

`make demo` 依次完成：生成 mock 湖 + 访问日志 → `scan` → 独立体积画像
`profile` → 带画像的 `evaluate` → 确定性 `plan`（容量压力参与决策）→
apply 前置校验 → 确认后 `--apply` → Markdown/JSON 报告 →
`rollback` → 回滚后报告。所有产物位于 `data/artifacts/`，台账位于
`data/ledger/tier_triage.sqlite`。不带 `--profile` 的旧命令仍可单独使用，
结论与旧版本一致。

## 湖目录布局

采用 `tiered-v1` 布局：层级目录 + `db/table/key=value/...` 分区。

```text
data/lake/
├─ hot/sales/orders/dt=2026-10-28/part-0000.json
├─ warm/sales/orders/dt=2025-05-01/part-0000.json
├─ cold/sales/orders/dt=2024-01-01/part-0000.json
└─ hot/ops/audit_log/region=cn/dt=2026-10-20/part-0000.json
```

分区可以单级 `dt=YYYY-MM-DD`，也可以多级 `region=cn/dt=...`。扫描忽略符号
链接；非法目录、表根散落文件等进入 `unparsed` 列表，不中断整体扫描。

独立的**分区体积画像**（`profile` 命令 / `scan --profile-out`）在扫描阶段
顺带实测每个分区的字节数、文件数和平均文件大小，产出单独的
`volume_profile.json`（带 `as_of`、稳定排序与 `profile_hash`）。符号链接
和无法解析的目录不计体积；缺文件或权限失败的分区进入画像异常列表并标记
`incomplete`/`unreadable`，不会中断扫描。画像的容量语义与容量紧张时的取舍
见 [docs/CAPACITY_PROFILE.md](docs/CAPACITY_PROFILE.md)。

## 配置：`config/policy.yaml`

`version` 固定为 `1`，`windows` 必须包含 `30`，`defaults` 的十个字段必填。
表规则使用 `fnmatch` 通配符；`priority` 越大优先级越高，逐字段覆盖 defaults，
同级按 `match` 字典序确定最终覆盖顺序。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `hot_retain_days` | int | 最小热层保留期；满足则强制 `hot` |
| `hot_min_reads_30d` | int | 30 天读次数低于此值则至少降为 `warm` |
| `warm_after_days` | int | 分区年龄达到此值则至少降为 `warm` |
| `cold_after_days` | int | 年龄达到此值且低读则降为 `cold` |
| `cold_max_reads_30d` | int | 冷层允许的最大 30 天读次数 |
| `purge_after_days` | int | 可进入 purge 候选的最小年龄 |
| `purge_idle_days` | int | 无访问或空闲达到此值才可 purge |
| `purge_max_reads_30d` | int | purge 候选的最大 30 天读次数 |
| `purge_allowed` | bool | 是否允许该表族产生 purge 候选 |
| `max_hot_bytes` | int | 分区超过此字节数时不得留在 hot |
| `max_warm_bytes` | int | 可选 warm 容量预算（0/缺省不启用）；接入画像时超预算的 warm 下沉按缓解顺序阻断 |

另有 `config/policy_strict.yaml`（严格保留）和 `config/tables.yaml`
（表登记说明）。字段语义和边界见 [docs/LIFECYCLE_POLICY.md](docs/LIFECYCLE_POLICY.md)，
体积画像如何改变分层落点见 [docs/CAPACITY_PROFILE.md](docs/CAPACITY_PROFILE.md)。

## 常用命令

```bash
# 扫描并记录快照
python -m tier_triage scan --lake data/lake --out data/artifacts/inventory.json

# 独立分区体积画像（可选 --as-of；省略则为纯物理快照）
python -m tier_triage profile --lake data/lake \
  --as-of 2026-11-01T00:00:00Z --out data/artifacts/volume_profile.json

# 固定评估时钟，保证结果可复现；--profile 可选，不传时为旧的年龄/读次数行为
python -m tier_triage evaluate --lake data/lake \
  --usage data/usage/access.jsonl --policy config/policy.yaml \
  --as-of 2026-11-01T00:00:00Z \
  --profile data/artifacts/volume_profile.json \
  --out data/artifacts/evaluation.json \
  --explain sales.orders.dt=2026-09-05

# 构建确定性计划（dry-run）；接入画像后容量阈值可改变分层落点
python -m tier_triage plan --lake data/lake \
  --usage data/usage/access.jsonl --policy config/policy.yaml \
  --as-of 2026-11-01T00:00:00Z \
  --profile data/artifacts/volume_profile.json \
  --out data/artifacts/plan.json

# 默认 dry-run；必须同时给 --apply 和 --yes 才执行移动
python -m tier_triage apply --plan data/artifacts/plan.json \
  --lake data/lake --db data/ledger/tier_triage.sqlite
python -m tier_triage apply --plan data/artifacts/plan.json \
  --lake data/lake --db data/ledger/tier_triage.sqlite --apply --yes

# 回滚与报告
python -m tier_triage rollback --batch <batch_id> --lake data/lake \
  --db data/ledger/tier_triage.sqlite
python -m tier_triage report --evaluation data/artifacts/evaluation.json \
  --plan data/artifacts/plan.json --db data/ledger/tier_triage.sqlite \
  --out data/artifacts/report
```

## 演示输出片段

```text
DEMO COMPLETE
  plan_hash:  35aec36de9275cced816d1da8e28cfda9cf4d55ca0e8bd00988855d65eb5d112
  batch:      batch-...
  apply:      completed (10 moved, 2 purge marks)
  rollback:   rolled_back (10 restored)
  reports:    data/artifacts/report_after_apply.md
              data/artifacts/report_after_rollback.md
```

`report_after_apply.md` 包含分层分布、预计下沉/清退字节、被阻断项、purge
清退清单、批次动作状态和回滚记录。

## 确定性、apply 与回滚

- **时钟注入**：`--as-of` 决定访问窗口、分区年龄与输出中的 `as_of`；不使用
  系统当前时间参与计划内容。
- **稳定排序**：分区按 `partition_id`；计划动作按动作类型、状态、表、分区
  排序后赋 `seq`。
- **计划哈希**：`sha256` 覆盖除 `plan_hash`/`plan_id` 外的完整计划。apply
  前重新计算；被人工改动、哈希不符时拒绝执行。
- **前置校验**：所有 ready 动作的源存在、目标不存在、目标路径不重复、目标
  父目录可用、按设备聚合的磁盘余量（默认预留 1 MiB）全部通过后才开始。
- **失败即回退**：中途失败时按逆序还原本批次已移动目录，清理本次创建的空
  目录，批次标记 `failed`。
- **回滚**：`rollback --batch <id>` 仅接受 `completed` 批次；目标被外部改动
  时记为 `unrecoverable`，不会覆盖任何现有路径。详见
  [docs/ROLLBACK.md](docs/ROLLBACK.md)。

## 目录结构

```text
tier-triage/
├─ README.md
├─ docs/                 DESIGN / LIFECYCLE_POLICY / ROLLBACK
├─ pyproject.toml        setuptools 配置与 `tier-triage` 入口
├─ Makefile              demo / test / serve / scan / evaluate / plan / clean
├─ config/               policy.yaml / policy_strict.yaml / tables.yaml
├─ scripts/              make_fixtures.py（幂等） / run_demo.py（一键演示）
├─ tier_triage/
│  ├─ cli.py             scan / profile / evaluate / plan / apply / rollback / report / serve
│  ├─ lake/              inventory / profile（体积画像） / partitioning / fs_ops
│  ├─ usage/             access_log / stats
│  ├─ policy/            loader / rules
│  ├─ decide/            scorer / planner
│  ├─ exec/              executor / rollback
│  ├─ audit/             ledger / report
│  └─ web/               local Flask app + templates
└─ tests/                tmp_path 隔离的单元与端到端测试
```

## 离线与安全声明

- 运行期零网络请求；服务只绑定 `127.0.0.1`，页面资源全部本地内联，无 CDN。
- 仓库不含 token、密码、真实公司名、真实域名或云厂商配置。
- 所有迁移路径先经 whitelist 检查，禁止绝对路径、`..` 穿越和跨设备移动。
- 不物理删除数据；失败回退只还原本批次动作并移除本批次创建的空目录。

## 明确留待后续

云对象存储生命周期 API、调度器/并发迁移、压缩转码、小文件合并、脱敏、质量校验、去重、权限体系、物理删除和 Web 实时图表均不在本版本范围。
