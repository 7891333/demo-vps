# 云端 Bash 命令执行（/api/exec 远程控制）使用文档

## 一、这是什么

`/api/exec` 是运行在 GitHub Actions 临时环境（云端）里的**远程控制接口**。
通过它，你可以在本地（任意地方）对云端实时执行任意 Bash 命令，就像直接 SSH 进那台机器一样。

**访问地址**：`https://ghvps.kekeke.cc.cd/api/exec`（固定域名）

---

## 二、工作原理（一句话）

云端 Flask 服务收到请求 → 校验令牌 → 用 `subprocess.run(cmd, shell=True)` 在云端执行命令 → 把 stdout/stderr 返回给你。

```
你的本地/任意地方
    │  POST https://ghvps.kekeke.cc.cd/api/exec
    │  body: {"token": "xxx", "cmd": "whoami"}
    ▼
Cloudflare 固定隧道（ghvps-demo）
    ▼
GitHub Actions 云端（Linux runner）
    ▼
Flask /api/exec 路由
    │  校验 EXEC_TOKEN
    │  subprocess.run("whoami", shell=True)
    ▼
返回 {"ok":true, "code":0, "stdout":"runner", "stderr":""}
```

---

## 三、请求格式

```
POST https://ghvps.kekeke.cc.cd/api/exec
Content-Type: application/json

{
  "token": "你的EXEC_TOKEN",
  "cmd": "要执行的命令"
}
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `token` | 是 | 远程控制令牌，与 GitHub Secret 里的 `EXEC_TOKEN` 一致 |
| `cmd` | 是 | 要执行的 Bash 命令，最长 2000 字符 |

### 返回格式

```json
{
  "ok": true,
  "code": 0,             // 命令退出码，0=成功
  "stdout": "...",        // 标准输出（最多保留 4000 字符）
  "stderr": "..."         // 错误输出（最多保留 2000 字符）
}
```

### 错误码

| HTTP | 含义 |
|------|------|
| 200 | 命令执行成功 |
| 403 | 令牌错误（未授权） |
| 400 | 命令为空 / 过长 |
| 500 | 命令执行超时(30s) 或异常 |

---

## 四、使用示例（curl）

### 1. 基础命令
```bash
curl -s -X POST https://ghvps.kekeke.cc.cd/api/exec \
  -H "Content-Type: application/json" \
  -d '{"token":"你的EXEC_TOKEN","cmd":"whoami && hostname && date"}'
```

### 2. 查看磁盘
```bash
curl -s -X POST https://ghvps.kekeke.cc.cd/api/exec \
  -H "Content-Type: application/json" \
  -d '{"token":"你的EXEC_TOKEN","cmd":"df -h / | tail -1"}'
```

### 3. 安装软件（比如装个工具）
```bash
curl -s -X POST https://ghvps.kekeke.cc.cd/api/exec \
  -H "Content-Type: application/json" \
  -d '{"token":"你的EXEC_TOKEN","cmd":"apt-get install -y htop"}'
```

### 4. 写文件
```bash
curl -s -X POST https://ghvps.kekeke.cc.cd/api/exec \
  -H "Content-Type: application/json" \
  -d '{"token":"你的EXEC_TOKEN","cmd":"echo hello > /tmp/test.txt && cat /tmp/test.txt"}'
```

### 5. 组合多条命令（用 && 或 ;）
```bash
curl -s -X POST https://ghvps.kekeke.cc.cd/api/exec \
  -H "Content-Type: application/json" \
  -d '{"token":"你的EXEC_TOKEN","cmd":"cd /home/runner/work/demo-vps/demo-vps && ls -la && python3 -V"}'
```

---

## 五、实现原理（核心代码）

```python
@app.route("/api/exec", methods=["POST"])
def exec_cmd():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    if not EXEC_TOKEN or token != EXEC_TOKEN:
        return jsonify(ok=False, error="未授权"), 403          # ① 令牌校验
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify(ok=False, error="命令为空"), 400         # ② 参数校验
    if len(cmd) > 2000:
        return jsonify(ok=False, error="命令过长"), 400
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return jsonify(ok=True, code=proc.returncode,
                       stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="命令执行超时(30s)"), 500
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
```

### 关键点说明
- **`shell=True`**：命令通过 shell 执行，支持管道 `|`、重定向 `>`、`&&` 等所有 shell 语法
- **`timeout=30`**：单条命令最多执行 30 秒，防止挂死
- **`capture_output=True`**：捕获 stdout/stderr，执行完统一返回
- **`text=True`**：以文本形式返回（而非字节）
- **输出截断**：stdout 保留后 4000 字符、stderr 保留后 2000 字符（防超长）

---

## 六、安全注意事项

1. **令牌必须保密**：`EXEC_TOKEN` 是最高权限（相当于云端 root shell），泄露=别人能控制你的云端。妥善保管，不要外传。
2. **接口暴露公网**：`/api/exec` 通过固定域名对外，任何人能访问，但**没有正确 token 一律 403**。
3. **建议定期更换**：如需更换令牌，在 GitHub 仓库 `Settings → Secrets → Actions` 更新 `EXEC_TOKEN` 即可，下次 job 生效。
4. **命令超时限制**：单条命令 30 秒超时，适合快速操作；长任务请用后台方式（如 `nohup ... &`）。

---

## 七、相关接口一览

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/exec` | POST | 远程执行 Bash 命令（本文档） |
| `/api/status` | GET | 查询运行状态、job_id、leader、URL 等 |
| `/api/health` | GET | 健康检查 |
| `/api/add` | POST | 留言板写入（仅 leader 可写） |
| `/api/backup` | POST | 手动立即加密备份（仅 leader） |