# CI/CD 与生产部署

本项目的生产配置与原有开发配置分离。新增文件不会替换 `docker-compose.yml`、现有 Dockerfile 或业务代码。

## 流水线

### CI

`.github/workflows/ci.yml` 在发往 `main` 的 Pull Request、`main` 分支推送以及手动触发时运行：

1. 后端安装 `backend/requirements.txt`，编译 Python 源码并运行全部 Pytest。
2. 前端使用 `npm ci`，检查 API 超时约束，执行 TypeScript 检查和 Vite 生产构建。
3. 使用两个生产 Dockerfile 构建前后端镜像，保证镜像定义可用。
4. 展开并校验 `compose.production.yml`。

建议将 CI 的四类检查设为 `main` 分支的必需状态检查。

### CD

`.github/workflows/cd.yml` 在 `main` 的 CI 成功后自动运行，也支持手动指定 Git ref：

1. 以提交 SHA 构建前后端镜像并推送到 GHCR；镜像标签不可变，例如 `sha-0123456789ab`。
2. 为镜像生成 SBOM 与构建来源证明。
3. 进入 GitHub `production` Environment；建议配置 required reviewer 和仅允许 `main` 部署。
4. 通过已校验的 SSH host key 上传 Compose、环境文件和运维脚本。
5. 拉取镜像，启动 PostgreSQL/Redis，执行 Alembic 迁移，再启动应用。
6. 校验容器健康状态、反向代理 `/api/health` 和 worker 进程；失败时恢复上一版应用配置。

数据库迁移可能不是可逆的，因此发布迁移必须保持向后兼容；重大迁移前先做备份。

## GitHub 配置

在仓库 Settings → Environments 新建 `production`，建议启用审批和 `main` 分支限制。

Environment secrets：

| 名称 | 说明 |
| --- | --- |
| `PROD_SSH_HOST` | 生产服务器域名或 IP |
| `PROD_SSH_PORT` | SSH 端口，通常为 `22` |
| `PROD_SSH_USER` | 有 Docker 权限的非 root 部署用户 |
| `PROD_SSH_PRIVATE_KEY` | 部署专用私钥 |
| `PROD_SSH_KNOWN_HOSTS` | 预先核验的服务器 known_hosts 行，不要在流水线中临时信任扫描结果 |
| `PROD_ENV_FILE` | 参考 `.env.production.example` 的完整生产环境文件；可省略三个由流水线管理的镜像/版本字段 |

Environment variables：

| 名称 | 说明 |
| --- | --- |
| `PRODUCTION_DEPLOY_ENABLED` | 完成服务器、Secrets 和依赖风险检查后设为 `true`；默认不执行远程部署 |
| `PROD_DEPLOY_PATH` | 服务器部署目录，默认 `/opt/trip-planner` |
| `PROD_APP_URL` | 对外 HTTPS 地址，例如 `https://trip.example.com`；填写后 CD 会进行公网健康检查 |

Repository variables（前端构建时会公开到浏览器，不能视为服务端秘密）：

| 名称 | 说明 |
| --- | --- |
| `VITE_AMAP_JS_KEY` | 高德 JS API Key |
| `VITE_AMAP_SECURITY_CODE` | 高德 JS API 安全密钥 |

生成服务器 host key 记录时应在可信网络或控制台核对指纹，再保存输出：

```bash
ssh-keyscan -p 22 your-server.example.com
```

## 服务器准备

最低要求：Linux、Docker Engine、Docker Compose v2、Bash、至少 2 CPU/4 GB RAM，以及一个可执行 Docker 命令的非 root 部署用户。服务器必须能访问 GHCR 和项目使用的外部 API。

应用默认只监听 `127.0.0.1:8080`，应由负载均衡器或宿主机反向代理终止 TLS。`ops/Caddyfile.example` 给出了 Caddy 示例；替换域名后再启用。防火墙只需公开 80/443 和受限来源的 SSH，PostgreSQL、Redis 与后端端口都不会发布到宿主机。

首次部署前，确保 `PROD_ENV_FILE` 至少替换：

- `POSTGRES_PASSWORD`、`REDIS_PASSWORD`：长且 URL-safe 的随机值。
- `ENCRYPTION_KEY`：有效 Fernet key。
- `FRONTEND_ORIGIN`：生产站点的 HTTPS Origin。
- 各外部服务 API Key；若不使用外部服务，将 `ENABLE_EXTERNAL_SERVICES=false` 和 `AMAP_MCP_ENABLED=false`。

生产镜像已固定安装 `@amap/amap-maps-mcp-server@0.0.8`，环境模板通过 `mcp-amap` 直接启动，避免容器运行时从 npm 拉取浮动的 latest 版本。模板默认关闭 MCP 并使用现有 HTTP fallback，因为项目锁定的 `hello-agents 0.2.9` 限制 FastMCP `<3.0`，而当前修复版本为 3.x；只有在完成兼容升级或风险评估后再开启 MCP。

生成 Fernet key：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 数据与恢复

PostgreSQL 和 Redis 使用独立命名卷，容器重建不会删除数据。生产环境仍需异机备份；以下命令生成 PostgreSQL 自包含格式备份与 SHA-256 校验文件：

```bash
/opt/trip-planner/backup-postgres.sh /opt/trip-planner /var/backups/trip-planner
```

建议由 systemd timer/cron 定时运行，并将备份同步到受控对象存储，设置保留策略并定期演练恢复。脚本不会自动删除旧备份。

常用检查：

```bash
cd /opt/trip-planner
docker compose --env-file .env.production -f compose.production.yml ps
docker compose --env-file .env.production -f compose.production.yml logs --tail=200 backend worker
curl --fail http://127.0.0.1:8080/api/health
```

手动重新部署当前配置：

```bash
cd /opt/trip-planner
docker compose --env-file .env.production -f compose.production.yml pull
docker compose --env-file .env.production -f compose.production.yml up -d --remove-orphans --wait
```

## 生产基线覆盖

- 质量门禁：后端测试、前端检查、镜像构建、Compose 校验。
- 供应链：GHCR 私有/公开镜像、提交 SHA 标签、SBOM、来源证明、Dependabot。
- 运行安全：应用容器非 root、只读根文件系统、移除 Linux capabilities、禁止提权、内部数据网络。
- 可用性：健康检查、自动重启、优雅停止、部署并发锁、上线后探活和应用版本回退。
- 资源治理：CPU、内存、PID 和容器日志轮转限制。
- 数据：PostgreSQL/Redis 持久卷、Alembic 发布迁移、独立备份脚本。
- 网络：默认仅回环监听，推荐 TLS 终止，不暴露数据库、Redis 和后端端口。
