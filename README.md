# GitHub Actions 临时环境加密持久化演示

利用 **GitHub Actions 的 schedule 定时唤醒** + **GitHub Releases 永久存储**，
实现**无需本地守护进程**的云端自动续命与数据加密持久化。

## 核心思路

```
GitHub Actions 每 6 小时自动醒来（schedule cron 触发，不需要本地守护进程）
  ├─ ① 从 GitHub Releases 拉取 AES-256-GCM 加密备份
  ├─ ② 用 GitHub Secrets 中的密钥解密，打开数据库
  ├─ ③ 运行演示站点（本 job 生命周期内）
  ├─ ④ 后台线程每 45 秒把数据库加密后上传回 Releases
  └─ ⑤ 6 小时到点销毁，数据已安全存在 Releases
        ↓
  下个 schedule 自动醒来，重复 ①→⑤
```

## 为什么安全

- **public 仓库免费无限跑**（private 有分钟配额限制）
- 数据库在 Releases 中是 **AES-256-GCM 加密后的密文**，公开也无妨
- 密钥只存在 **GitHub Secrets**，GitHub 加密保管，永不落盘/入库
- 仓库被扒走 = 一堆密文，没有密钥毫无价值

## 文件说明

| 文件 | 作用 |
|------|------|
| `demo_server.py` | 云端核心：拉取/解密/启动站点/定期加密备份/挂隧道 |
| `.github/workflows/demo.yml` | workflow：定时唤醒 + 环境 + 运行 |
| `requirements.txt` | Python 依赖 |

## 所需 Secrets

| Secret | 说明 |
|--------|------|
| `DEMO_KEY` | AES-256 加密密钥（hex，64位） |
| `GH_TOKEN` | 有 repo 权限的 GitHub Token |

## 手动触发

```bash
# 手动触发一次
curl -X POST https://api.github.com/repos/7891333/demo-vps/actions/workflows/demo.yml/dispatches \
  -H "Authorization: token <GH_TOKEN>" \
  -H "Accept: application/vnd.github.v3+json" \
  -d '{"ref":"main"}'
```