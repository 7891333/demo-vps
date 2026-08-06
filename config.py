# -*- coding: utf-8 -*-
"""全局配置：集中管理所有环境变量与运行参数"""
import os

# ==================== 运行角色 ====================
# INSTANCE_ROLE: manager=管理实例 / worker=工作实例
ROLE = os.environ.get("INSTANCE_ROLE", "worker")

# ==================== 实例标识 ====================
INSTANCE_ID = os.environ.get("INSTANCE_ID", "worker-1")  # 工作实例唯一 ID

# ==================== GitHub ====================
REPO = os.environ.get("REPO", "7891333/demo-vps")
GH_TOKEN = os.environ.get("GH_TOKEN", "")

# ==================== 安全 ====================
DEMO_KEY = os.environ.get("DEMO_KEY", "")          # AES-256 加密密钥
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")      # 远程控制/终端令牌

# ==================== 隧道 ====================
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")
TUNNEL_HOST = os.environ.get("TUNNEL_HOST", "")    # 工作实例固定域名
CF_EMAIL = os.environ.get("CF_EMAIL", "")
CF_API_KEY = os.environ.get("CF_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "")      # 主域名 zone
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "kekeke.cc.cd")  # 固定域名主域名

# ==================== Releases 存储 ====================
BACKUP_TAG = "backup"
ASSET_DB = "demo.db.enc"
ASSET_FILES = "files.tar.gz.enc"
ASSET_LEADER = "leader.json"
ASSET_ACCOUNTS = "accounts.json.enc"   # 账号配置（加密）
ASSET_INSTANCES = "instances.json.enc" # 实例清单（加密）

# ==================== 主 job 锁 ====================
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "60"))

# ==================== 数据/文件 ====================
DB_FILE = "demo.db"
FILES_DIR = os.path.expanduser(os.environ.get("FILES_DIR", "~/files"))

# ==================== 服务 ====================
PORT = int(os.environ.get("PORT", "8080"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "45"))

# ==================== 无缝衔接 ====================
PRE_WAKE_SECONDS = int(os.environ.get("PRE_WAKE_SECONDS", "21000"))

# follower 检查 leader 过期的间隔（快速升级，缩短交接缝）
FOLLOWER_CHECK = int(os.environ.get("FOLLOWER_CHECK", "15"))
# 任务持久化 asset（manager 后台任务队列）
ASSET_TASKS = "tasks.json.enc"

# ==================== WSS 终端会话 ====================
SESSION_TTL = int(os.environ.get("SESSION_TTL", "300"))

# ==================== 管理实例 ====================
# 每个账号最大并发 job 数（GitHub 免费账号 public 仓库限制）
DEFAULT_MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "20"))
# 管理实例自己的 workflow 名（用于续命）
MANAGER_WORKFLOW = os.environ.get("MANAGER_WORKFLOW", "manager.yml")
WORKER_WORKFLOW = os.environ.get("WORKER_WORKFLOW", "worker.yml")

# ==================== Cloudflare 隧道 ====================
# 固定域名前缀，如 inst1.ghost.kekeke.cc.cd
TUNNEL_PREFIX = os.environ.get("TUNNEL_PREFIX", "ghost")
# 管理实例固定域名
MANAGER_HOST = os.environ.get("MANAGER_HOST", "manager.kekeke.cc.cd")

# ==================== 自动更新 ====================
# 主仓库（上游），用于版本对比和 fork 同步
MAIN_REPO = os.environ.get("MAIN_REPO", "7891333/demo-vps")
# 当前 checkout 的 commit SHA（由 workflow 传入）
CURRENT_SHA = os.environ.get("CURRENT_SHA", "")
