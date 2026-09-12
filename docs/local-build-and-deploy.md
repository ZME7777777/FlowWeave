# 本地构建与部署

这是 FlowWeave 本地 Docker Compose 部署的兼容入口，保留该文件路径供仓库检查和运维流程使用。当前说明在[部署与运维](deployment.md)；本地首次启动见[本地启动](getting-started.md)，环境变量见[环境配置](environment-reference.md)。

## 最短命令

```bash
cp .env.example .env
# 设置工作区绝对路径、网络模式和两个不同的控制器密钥
make install
make infra-up
```

验证：

```bash
docker compose --env-file .env -f infra/compose.yaml ps -a
curl -fsS http://127.0.0.1:8080/health/ready
curl -I http://127.0.0.1:5173/
```

日常停止使用 `make infra-down`。不要使用 `docker compose down -v`，它会删除持久数据。
