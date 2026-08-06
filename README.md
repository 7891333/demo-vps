# GitHub Actions 多实例管理器

利用 **GitHub Actions 临时环境** + **GitHub Releases 加密存储**，实现：
- **一键创建多个云端工作实例**（纯 API）
- **多 GitHub 账号支持**（一个账号 20 并发，N 个账号 = N×20）
- 每个实例：WSS 交互式终端 + API 命令执行 + 文件持久化 + 自动续命
- 生产级模块化架构

## 架构

```
【管理实例 manager】总管家（固定域名 ghvps.kekeke.cc.cd）
  ├─ 账号池管理（多账号 + 自动负载均衡 + 并发检测）
  ├─ 实例创建/关闭/查询 API
  └─ 自动续命（每6小时无缝衔接）

【工作实例 worker × N】（instN.ghvps.kekeke.cc.cd）
  ├─ WSS 交互式终端（bytes 传输无乱码 + pyte 干净屏幕）
  ├─ API 命令执行（带超时）
  ├─ 文件持久化（~/files 目录）
  └─ 自动续命（除非手动关闭）
```

## 快速开始

### 1. 配置 Secrets（管理仓库）
| Secret | 说明 |
|--------|------|
| `GH_TOKEN` | 管理账号 GitHub Token |
| `DEMO_KEY` | AES-256 加密密钥（hex 64位） |
| `EXEC_TOKEN` | 远程控制/终端令牌 |
| `TUNNEL_TOKEN` | Manager 固定隧道凭证 |
| `CF_EMAIL` / `CF_API_KEY` | Cloudflare 账号 |
| `CF_ACCOUNT_ID` / `CF_ZONE_ID` | Cloudflare 区域 |

### 2. 触发管理实例
```bash
gh workflow run manager.yml --repo <owner>/demo-vps
```

### 3. 添加工作账号
```bash
curl -X POST https://ghvps.kekeke.cc.cd/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"name":"acc1","token":"ghp_xxx","repo":"<owner>/demo-vps","max_concurrency":20}'
```

### 4. 一键创建实例
```bash
curl -X POST https://ghvps.kekeke.cc.cd/api/instances
# → {"ok":true,"instance":{"id":"inst1","hostname":"inst1.ghvps.kekeke.cc.cd",...}}
```

### 5. 连接终端
```bash
python3 ghvss_cli.py <EXEC_TOKEN> https://inst1.ghvps.kekeke.cc.cd
```

## 管理 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 查看账号和实例总览 |
| `/api/accounts` | GET/POST | 查看/添加账号 |
| `/api/accounts/<name>` | DELETE | 删除账号 |
| `/api/instances` | POST/GET | 创建/查看实例 |
| `/api/instances/<id>` | GET/DELETE | 查看/关闭实例 |
| `/api/instances/<id>/exec` | POST | 在实例上执行命令（带超时） |

## 工作实例 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/exec` | POST | 命令执行（token + timeout） |
| `/api/term/screen` | GET | pyte 干净屏幕文本 |
| `/api/backup` | POST | 手动备份 |
| `/api/status` | GET | 实例状态 |
| `/socket.io` | WSS | 交互式终端 |

## 多账号并发

- 每个账号最大并发 = `max_concurrency`（默认 20）
- 创建实例时自动负载均衡（选余量最多的账号）
- 全部账号满时返回"并发已满"错误
- 动态添加账号：`POST /api/accounts`

## 持久化

- 数据库 + `~/files/` 文件目录，每 45 秒加密备份到 Releases
- 每个实例数据独立（按实例 ID 隔离 asset）
- job 销毁自动恢复，除非手动关闭实例