# 任务迭代与验收流程

## 状态流转
1. `pending`：任务已创建，等待执行
2. `processing`：执行中
3. `pending_acceptance`：执行完成，等待用户验收
4. `accepted`：用户验收通过
5. `archive`：仅 `accepted` 任务允许归档
6. `rejected`：用户验收拒绝，任务保留在结果区继续迭代

## 验收门禁
- 任务执行结束后必须写入验收文档和验收用例
- 未执行 `/accept <task_id|代号>` 前，禁止归档
- 执行 `/reject <task_id|代号> <原因>` 后维持可查询状态，可继续修复后再次验收

## 查询命令
- `/status`：查看活动任务，含验收标识
- `/status <task_id|代号>`：查看单任务状态、验收标识、下一步命令、验收文档路径
- `/accept <task_id|代号>`：验收通过并归档
- `/reject <task_id|代号> <原因>`：验收拒绝，不归档

## 任务完成产物
- `shared-volume/codex-tasks/results/<task_id>.json`
- `shared-volume/codex-tasks/logs/<task_id>.run.json`
- `shared-volume/codex-tasks/acceptance/<task_id>.acceptance.md`
- `shared-volume/codex-tasks/acceptance/<task_id>.cases.json`
