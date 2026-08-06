# -*- coding: utf-8 -*-
"""全局配置：所有环境变量在此集中管理"""
import os

# ==================== GitHub ====================
REPO = os.environ.get("REPO", "7891333/demo-vps")
GH_TOKEN = os.environ.get("GH_TOKEN", "")

# ==================== 安全 ====================
DEMO_KEY = os.environ.get("DEMO_KEY", "")          # AES-256 加密密钥
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")      # 远程控制/终端令牌

# ==================== 隧道 ====================
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")
TUNNEL_HOST = os.environ.get("TUNNEL_HOST", "ghvps.kekeke.cc.cd")

# ==================== Releases 资产 ====================
BACKUP_TAG = "backup"
ASSET_DB = "demo.db.enc"          # 数据库加密备份
ASSET_FILES = "files.tar.gz.enc"  # 文件目录加密备份
ASSET_LEADER = "leader.json"      # 主job锁心跳

# ==================== 主 job 锁 ====================
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "90"))

# ==================== 数据/文件 ====================
DB_FILE = "demo.db"
FILES_DIR = "files"

# ==================== 服务 ====================
PORT = int(os.environ.get("PORT", "8080"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "45"))

# ==================== 无缝衔接 ====================
PRE_WAKE_SECONDS = int(os.environ.get("PRE_WAKE_SECONDS", "21300"))

# ==================== WSS 终端会话 ====================
# 断线后会话保留时长（秒），超时未重连则销毁
SESSION_TTL = int(os.environ.get("SESSION_TTL", "300"))