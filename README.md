# FlowWeave

FlowWeave 是研发团队的 Agent 控制面：将可复用的节点能力、模型服务、环境与流程组织为可审计的运行，并把实际执行交给 OpenHands Runtime。它不重造 Agent 执行器；它负责版本冻结、权限、策略、人工门禁、资源隔离和审计。

从这里开始：

- [平台概览](docs/platform-overview.md)：产品模型、系统边界和仓库结构。
- [本地启动](docs/getting-started.md)：开发环境和 Docker Compose 的最短可用路径。
- [环境配置](docs/environment-reference.md)：`.env` 变量、密钥和工作区说明。
- [部署与运维](docs/deployment.md)：本地重建、定向更新、远端发布和验证。

## 快速启动

依赖：Docker、Node.js 22+、pnpm 10.33.4、Python 3.12+ 和 uv。

```bash
cp .env.example .env
# 编辑 .env：设置工作区绝对路径及两个不同的控制器密钥
make install
make infra-up
```

打开 <http://localhost:5173>。详情与生产前配置见[本地启动](docs/getting-started.md)和[环境配置](docs/environment-reference.md)。

## 维护说明

当前实现和迁移是运行事实源。`docs/flowrun-openhands-runtime-design.md` 与
`docs/flowrun-runtime-task-progress.md` 是保留的 Runtime 架构决策与实施记录；它们不替代上述日常使用文档。
