# 冷热分诊台 · 审计报告

- 评估时钟（as-of）：`2026-11-01T00:00:00Z`
- 计划：`plan-35aec36de9275cce`
- 计划哈希：`35aec36de9275cced816d1da8e28cfda9cf4d55ca0e8bd00988855d65eb5d112`
- 策略指纹：`fa4e21359265a31a99fd48ee55f5d6aca1ba3b8ecdee66226eb3412142ad45a8`
- 清单哈希：`3faf96daf9b6094198acc07e9931417cee31670bf28e392c07d8b1007141f634`

## 分层分布

| 目标层 | 分区数 |
| --- | ---: |
| hot | 5 |
| warm | 5 |
| cold | 8 |
| purge_candidate | 2 |

## 空间

- 预计下沉/清退字节：**1.4 KiB**
- purge 清退候选（仅标记）：**191 B**

> 本工具不做物理删除；purge 仅进入复核清单。

## 被阻断项

无。

## purge 清退清单

| 分区 | 文件数 | 字节 | 原因 |
| --- | ---: | ---: | --- |
| `marketing.events.dt=2025-07-01` | 1 | 96 | decision=purge_candidate; score=0 |
| `marketing.events.dt=2026-05-01` | 1 | 95 | decision=purge_candidate; score=0 |

## 批次摘要

- 批次：`batch-20261113T130308Z-428092`
- 状态：**rolled_back**
- 动作：12（移动 10，标记 2）
- 操作者：`cli`
- 动作状态：{'rolled_back': 10, 'skipped': 8, 'unmarked': 2}
- 回滚记录：
  - 还原 10，不可恢复 0，时间 2026-11-13T13:03:08Z

## 复现说明

计划哈希覆盖评估时钟、策略指纹、清单哈希与全部动作。相同输入、策略与时钟应产生同一哈希。
