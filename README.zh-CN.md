# CPA Linux 自动升级执行器

执行器只使用固定版本清单和本机 Docker API，不保存 GitHub 写入凭据。`upgrader.py` 兼容 Python 3.6，协议目录为 `catalog / requests / status`，Manager 只读挂载 catalog/status，requests 由宿主执行器写入。

安装脚本从当前运行容器的 Compose labels 识别项目、工作目录、容器名、端口和数据挂载；不扫描其他目录，不读取 OAuth/管理密钥内容。首次安装保持 `enabled=false`。验收完成后执行 `python3 /opt/cpa-platform/upgrader/install.py --enable` 开启每日 05:00–06:00（Asia/Shanghai）安装窗口。

每小时准备公开 channel 中的固定镜像；每日按 CLI → Manager 串行处理，每个组件每窗口只推进一个兼容边。安全检查、备份、Docker 切换、180 秒启动检查和最多 30 分钟后台迁移都记录在 journal。超时、未知状态、破坏性迁移、下载/构建失败会暂停并保留现场。

服务入口：

- `GET /usage-service/upgrades`：原有 catalog / queue / status。
- `GET /usage-service/upgrades/automation`：只读自动升级开关、北京时间下次执行、最近结果、准备/暂停原因。

执行器服务使用 `cpa-upgrader.service`，不授予 Manager Docker socket；Manager 容器仅获得三条协议目录挂载。
