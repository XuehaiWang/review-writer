# Review Writer

一个基于论文原文证据的学术综述写作工具。从上传 PDF、检索和规划章节，到正文修订、图像处理及 Word/PDF 导出，在同一个工作台完成。

重点支持化学综述，也可用于其他学科。AI 生成内容仍需作者核对科学事实与引用。

## 主要功能

- **文献管理**：支持多选 PDF、文件夹（含子目录）和 ZIP 导入，使用 MinerU 逐篇解析全文、图片和书目信息。可查看进度、取消剩余；重复文件按内容识别，不覆盖已有书目信息。单篇 PDF 上限 80 MB，ZIP 上限 256 MB、300 个 PDF、展开后 1 GB；不支持加密或嵌套压缩包。
- **检索与规划**：按主题检索论文，整理证据，推荐或自定义章节大纲。
- **章节写作**：结合章节任务和论文证据生成正文，保留引用来源。
- **图像处理**：选择论文图片、AI 重绘，以及在线 SVG/Ketcher 编辑。
- **初稿修订**：批量优化、章节对话和手动编辑；修改候选确认后才保存，支持版本查看与恢复。
- **终稿导出**：组装已确认的初稿，检查出版信息并下载 Word/PDF。摘要、结论、总览图和正文修改统一在初稿完成，旧终稿保留查看和下载。
- **多用户管理**：隔离用户数据，保存任务进度；管理员统一配置模型服务、查看用量和故障记录。

## 使用流程

文献库 → 检索筛选 → 分析与大纲 → 章节生成 → 图像处理 → 初稿修订 → 终稿导出

## 快速部署

需要 Git、Docker Desktop 或 Docker Engine（含 Compose v2）。以下命令以 PowerShell 为例。

### 1. 获取代码

```powershell
git clone --branch dy-launch https://github.com/LCHTTTT/review-writer.git
Set-Location review-writer
Copy-Item .env.hosted.example .env.hosted
```

### 2. 填写配置

按 `.env.hosted` 中的注释设置：

- 数据库密码、凭据加密密钥、内部 Worker 和 PDF 服务令牌；不要保留示例占位值。
- 管理员邮箱与 SMTP 邮件服务，用于注册验证码和密码重置。
- 文本、图像、向量和 MinerU 服务。启动后也可在管理员页面管理服务配置。
- 访问地址、端口及发布版本号 `REVIEW_WRITER_IMAGE_TAG`（每次发布使用新值）。

加密密钥可用下面的命令生成；内部令牌请分别生成，不要共用：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env.hosted` 含敏感信息，不要上传 Git，也不要随意更换已使用的加密密钥。

### 3. 启动

```powershell
docker compose --env-file .env.hosted build api pdf-renderer
docker compose --env-file .env.hosted up -d --no-build
docker compose --env-file .env.hosted ps
```

默认访问 [http://127.0.0.1:8770](http://127.0.0.1:8770)。使用配置的管理员邮箱注册并登录，再检查模型与解析服务是否可用。

局域网访问需将 `REVIEW_WRITER_BIND_ADDRESS` 改为 `0.0.0.0`，并把 `REVIEW_WRITER_PUBLIC_ORIGIN` 设为实际访问地址。公网应配置 HTTPS 接入，并启用 `REVIEW_WRITER_SESSION_COOKIE_SECURE=true`；仅修改访问地址不会自动开启 HTTPS。

## 更新与数据保护

更新前先备份并等待运行中的任务完成。执行 `git pull origin dy-launch`，更换发布版本号，再执行上面的构建和启动命令。

- API、模型网关、迁移和所有 Worker 使用同一应用镜像；PDF 服务使用同一发布标签。
- 同时备份 `postgres_data`、`review_state` 两个持久卷及加密密钥。
- 验证服务正常后再清理未使用的本项目旧镜像。**不要执行 `docker compose down -v`，它会删除持久数据。**

查看服务日志：

```powershell
docker compose --env-file .env.hosted logs --tail 100 api worker model-gateway
```

## 技术与代码

前端使用 React + TypeScript，后端使用 FastAPI。PostgreSQL 保存业务和任务状态，SQLite 向量库支持语义检索，独立 Worker 执行耗时任务。

- `frontend/`：前端页面。
- `review_writer_api/`：接口、任务、模型网关与管理功能。
- `review_writer_core/`：证据、引用与写作公共逻辑。
- `skills/`：运行所需的脚本、提示词和模板。
- `review_writer_pdf_renderer/`：PDF 渲染服务。
- `tests/`、`review_writer_api/tests/`：后端测试；前端测试位于 `frontend/`。

仓库不包含用户论文、运行数据、密钥和本地开发文档。技能说明及模板是运行依赖，请保留。
