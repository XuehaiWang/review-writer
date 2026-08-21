# PostgreSQL 工作流迁移与回滚手册

本项目上线运行时只使用 PostgreSQL 保存用户、项目、七阶段状态、任务、版本、审批和审计数据。旧的 `workflow.sqlite3` 只作为一次性迁移源和只读回滚证据，正常 API 进程不会创建或打开它。

## 1. 自动迁移如何工作

`docker compose up` 的启动顺序固定为：

1. `postgres` 通过健康检查；
2. `migrate` 执行 Alembic 升级；
3. `migrate` 只读清点 `/app/.review-writer/hosted-workspaces` 中的旧 SQLite；
4. 若发现旧库，先写 inventory 与 dry-run 报告，再用 SQLite Backup API 生成校验过的独立备份，最后导入 PostgreSQL；
5. 校验通过并写入 `workflow_ready` 后，`api` 才启动。

没有旧库时按全新安装启动。已成功迁移时，入口会比较每个源的绝对路径和 SHA-256；完全一致则报告 `already_migrated`，不会重复导入。源变化、导入错误或缺失文件未获确认时，`migrate` 非零退出，API 保持关闭。

持久文件位于宿主机：

- `.review-writer/migration-reports/`：清点、干跑、正式迁移和 `latest.json`；
- `.review-writer/migration-backups/`：迁移器生成且通过 `PRAGMA integrity_check` 与表计数核验的 SQLite 副本；
- 原 `workflow.sqlite3`：不修改、不删除，迁移后保留为只读备份。

这两个目录可在 `.env.hosted` 中用 `REVIEW_WRITER_MIGRATION_REPORTS_DIR` 和 `REVIEW_WRITER_MIGRATION_BACKUPS_DIR` 改到服务器备份盘。

## 2. 首次上线

```powershell
Copy-Item .env.hosted.example .env.hosted
# 编辑密码、32 字节 Base64URL 加密密钥和访问地址
New-Item -ItemType Directory -Force .review-writer\migration-reports, .review-writer\migration-backups
docker compose --env-file .env.hosted config --quiet
docker compose --env-file .env.hosted up -d --build
docker compose --env-file .env.hosted ps
docker compose --env-file .env.hosted logs migrate
```

不要执行 `docker compose down -v`，否则会删除 PostgreSQL 与用户工作区数据卷。Linux 主机如果绑定目录不可写，应把两个迁移目录的所有者设为容器用户 UID `10001`。

若迁移源来自旧的单用户根目录，而不是按用户 UUID 分隔的托管目录，必须在 `.env.hosted` 设置 `REVIEW_WRITER_MIGRATION_OWNER_EMAIL`，且该邮箱用户已存在于 PostgreSQL。

## 3. 缺失文件处理

默认 `REVIEW_WRITER_MIGRATION_ACCEPT_MISSING_FILES=false`。此时数据库行可以被导入用于检查，但不会写入就绪标记，API 不会启动。

1. 打开 `.review-writer/migration-reports/latest.json`，逐项核查 `missing_files`；
2. 能恢复的文件先恢复到原路径，再重新启动 `migrate`；
3. 只有人工确认历史文件确实无法恢复且接受影响后，才把变量改为 `true`；
4. 再次 `docker compose up -d`，确认报告 `ready: true`。

确认缺失不等于伪造文件。迁移后的缺失 artifact 仍标记为不可用，相关最终稿发布门禁继续阻止引用不存在的文件。

### 历史文件哈希漂移

旧运行时允许不同历史 artifact version 指向同一个后来被原地改写的文件，因此旧台账的 `expected_sha256` 可能与停机时磁盘上的 `actual_sha256` 不同。这不是“缺文件”，必须用独立开关处理：

1. 保持 `REVIEW_WRITER_MIGRATION_ACCEPT_FILE_DRIFT=false` 完成首次演练；
2. 检查完整报告的 `drifted_files`，按项目、逻辑名、期望/实际哈希和大小确认来源；
3. 若旧字节已不存在且决定保留停机时实际内容，设置 `REVIEW_WRITER_MIGRATION_ACCEPT_FILE_DRIFT=true`；
4. 迁移器会为每个旧版本创建独立不可变副本，并在 metadata 同时保留旧版本 ID、旧期望哈希、迁移实际哈希和确认标记；不同历史版本使用不同 lineage，不会因实际字节相同而被丢弃。

该确认不能恢复旧运行时已经覆盖的历史字节，但能完整保存现存字节、旧台账证据和版本关系。不要用 `REVIEW_WRITER_MIGRATION_ACCEPT_MISSING_FILES` 代替它。

## 4. 手工清点和验证

```powershell
.\.venv\Scripts\python.exe -m review_writer_api.migrate_workflow inventory `
  --workspace-root .review-writer\hosted-workspaces `
  --report .review-writer\migration-reports\manual-inventory.json

.\.venv\Scripts\python.exe -m review_writer_api.migrate_workflow validate `
  --workspace-root .review-writer\hosted-workspaces `
  --report .review-writer\migration-reports\latest.json
```

容器内的 `latest.json` 是自动入口摘要；正式迁移的时间戳 `*-migration.json` 是完整校验报告。手工 `validate` 应针对完整报告，而不是摘要型 `fresh_install`/`already_migrated` 文件。

## 5. PostgreSQL 备份

在正式迁移前和迁移成功后各保存一次 PostgreSQL 备份：

```powershell
docker compose --env-file .env.hosted exec -T postgres `
  pg_dump -U review_writer -d review_writer -Fc `
  -f /tmp/postgres-before-migration.dump
docker cp review-writer-postgres-1:/tmp/postgres-before-migration.dump `
  .review-writer\migration-backups\postgres-before-migration.dump
Get-FileHash -Algorithm SHA256 `
  .review-writer\migration-backups\postgres-before-migration.dump
```

这里先让 `pg_dump` 在容器内写二进制文件，再用 `docker cp` 取回，避免 PowerShell 管道改变自定义格式 dump 的字节内容。若修改过 Compose 项目名，请先用 `docker compose ps` 确认 PostgreSQL 容器名并替换上述名称。

生产服务器可改用自己的备份系统。必须同时保存：PostgreSQL dump、`review_state` 工作区数据、SQLite 源与迁移器备份、迁移报告、部署时的 Git 提交号，以及长期不变的凭据加密密钥。

## 6. 回滚

回滚点为 Git 提交 `9eea953`，但仅切换代码不能回滚数据。正确顺序是：

1. 停止 API，保留故障现场与日志；
2. 备份当前 PostgreSQL 和 `review_state`；
3. 恢复迁移前 PostgreSQL dump 与对应工作区备份；
4. 切换代码到 `9eea953`；
5. 使用保留的原 SQLite 副本验证旧流程；
6. 不要覆盖 `dy` 分支，不要删除迁移报告与任何备份。

若只需修复新版本，应保持 API 停止，修复后重新执行迁移验证；只有报告成功、`workflow_ready` 有效、容器烟测通过后再恢复访问。

## 7. 上线验收

- `/api/v1/health` 返回 `status=ok`；
- 注册、退出、登录、创建项目和 Discovery 读取成功；
- 两个账号看不到彼此项目、任务、文件和 API 凭据；
- 局域网设备可打开 PDF、MinerU 图片、Ketcher 和最终 DOCX；
- 中英文切换覆盖七阶段按钮、状态与稳定错误码；
- `.review-writer/migration-reports/latest.json` 为 `fresh_install`、`already_migrated` 或 `ready: true`，且缺失/漂移确认与完整报告一致；
- API 运行期间没有新建 `workflow.sqlite3`，运行容器中没有 Prefect 服务或进程。
