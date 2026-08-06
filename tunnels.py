# -*- coding: utf-8 -*-
"""Cloudflare 隧道自动管理：创建/删除固定域名隧道（全自动，无需手动）"""
import json
import urllib.request
import urllib.error

import config


def cf_request(method, url, data=None, timeout=30):
    """Cloudflare API 请求，返回 (status, body)"""
    h = {
        "X-Auth-Email": config.CF_EMAIL,
        "X-Auth-Key": config.CF_API_KEY,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, method=method, headers=h)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content.decode() or "null")
        except Exception:
            return e.code, content.decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def create_tunnel(hostname, service_url="http://localhost:8080"):
    """
    创建固定域名隧道并配置 DNS。
    hostname: 完整域名，如 inst1.ghost.kekeke.cc.cd
    返回 (tunnel_id, tunnel_token)
    """
    if not all([config.CF_EMAIL, config.CF_API_KEY, config.CF_ACCOUNT_ID]):
        raise RuntimeError("CF 凭证未配置")
    name = "t-" + hostname.split(".")[0]
    # 1. 创建隧道
    status, d = cf_request("POST",
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel",
        data={"name": name, "config_src": "cloudflare",
              "config": {"ingress": [{"hostname": hostname, "service": service_url},
                                      {"service": "http_status:404"}]}})
    if status not in (200, 201):
        # 可能已存在同名隧道，尝试复用
        raise RuntimeError(f"创建隧道失败: {status} {d}")
    tid = d["result"]["id"]
    ttoken = d["result"]["token"]
    # 2. 单独更新 ingress（创建时 config 不生效的坑）
    cf_request("PUT",
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel/{tid}/configurations",
        data={"config": {"ingress": [{"hostname": hostname, "service": service_url},
                                      {"service": "http_status:404"}]}})
    # 3. 创建 DNS 记录
    if config.CF_ZONE_ID:
        cf_request("POST",
            f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records",
            data={"type": "CNAME", "name": hostname,
                  "content": f"{tid}.cfargotunnel.com", "proxied": True})
    return tid, ttoken


def delete_tunnel(tunnel_id, hostname):
    """删除隧道及 DNS 记录"""
    # 删除 DNS
    if config.CF_ZONE_ID:
        status, d = cf_request("GET",
            f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records?name={hostname}")
        if status == 200:
            for rec in d.get("result", []):
                cf_request("DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{config.CF_ZONE_ID}/dns_records/{rec['id']}")
    # 删除隧道
    if config.CF_ACCOUNT_ID:
        cf_request("DELETE",
            f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}/cfd_tunnel/{tunnel_id}")
    return {"ok": True}