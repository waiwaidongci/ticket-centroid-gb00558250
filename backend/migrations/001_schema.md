# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
  - 归口更正新增 `parent_run_id`（指向原始运行）、`revision_no`（修订序号，原始为 0）、
    `current_run_id`（当前生效版本指针）。重算版本不参与复核与交付列表，仅在指针切换后生效。
- `correction_batches` 归口更正事件链：以 `(run_id, review_no)` 和 `(run_id, revision_no)` 唯一。
  - `kind=correction` 携带逐件归口；`kind=withdrawal` 记录撤回。撤回将目标批次置为 `withdrawn`，
    重放剩余有效事件即恢复上一条有效归口。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核、更正和报表分发动作。

## 从初始快照升级

初始库没有更正相关列与表，需要手动执行（PostgreSQL）：

```sql
ALTER TABLE runs ADD COLUMN parent_run_id INTEGER REFERENCES runs(id);
ALTER TABLE runs ADD COLUMN revision_no INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN current_run_id INTEGER REFERENCES runs(id);
CREATE INDEX ix_runs_parent_run_id ON runs(parent_run_id);
CREATE INDEX ix_runs_current_run_id ON runs(current_run_id);
UPDATE runs SET current_run_id = id WHERE current_run_id IS NULL;

CREATE TABLE correction_batches (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    review_no VARCHAR(80) NOT NULL,
    revision_no INTEGER NOT NULL,
    kind VARCHAR(20) NOT NULL,
    entries JSON NOT NULL DEFAULT '[]',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    withdrawn_by VARCHAR(80),
    withdrawn_at TIMESTAMP,
    recalc_run_id INTEGER REFERENCES runs(id),
    note VARCHAR(500),
    author VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_correction_review_no UNIQUE (run_id, review_no),
    CONSTRAINT uq_correction_revision_no UNIQUE (run_id, revision_no)
);
CREATE INDEX ix_correction_batches_run_id ON correction_batches(run_id);
CREATE INDEX ix_correction_batches_status ON correction_batches(status);
```
