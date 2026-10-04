# DSH Remote Bridge 中继（backend）
**简体中文** | [English](README.en.md)

FastAPI + SQLite 中继：桌面插件主动出站连入（WSS），手机以 HTTPS + WSS 下指令；它只做鉴权、限速、转发与审计。

## 它是什么

- 一条**哑管道**：不解析、不落盘任何会话内容或模型输出；库里只有凭据哈希、设备行与审计摘要。
- 桌面**不开放任何入站端口**：插件是主动拨号方，所以家用 NAT / 没有公网 IP 也能用。
- 两条通道：插件 ↔ 中继 = WSS（`/api/v1/attach`）；手机 ↔ 中继 = HTTPS + SSE + WS。转发面是一张**白名单**（11 个 op），其余一律 `501 op_not_supported`；管理面没有 HTTP 接口，签发与吊销令牌都要在服务器上执行 CLI。

## 架构

```
手机 App ──HTTPS / WSS──▶ 中继 (FastAPI + SQLite) ◀──出站 WSS── 桌面插件 (DSH) ──▶ DSH Host
                              └─ var/relay.db：凭据哈希、设备行、审计摘要（不含会话内容）
```
中继只转发与记账，不读业务内容，也不会去连桌面。线协议、HTTP/事件接口与 op 白名单见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Debian 12 / Ubuntu 22.04 或更新（`deploy.sh` 只面向 Debian 系），systemd |
| Python | 3.11+（`deploy.sh` 用 `python3.11`） |
| 工具 | `curl`、`openssl`、`ss`（`iproute2`）、`git` |
| 网络 | 公网 IP（或反向代理）；开放一个对外端口（示例 `58443`），`22` 只放行你自己的 IP |

> Windows / macOS 也能跑（`python -m uvicorn app.main:app`），但没有 systemd；以下步骤面向 Linux。

## 部署

两条路，结果相同：**A** 按第 1–8 步手动做一遍（每步都有命令与预期输出）；**B** 跑第 9 步的 `deploy.sh`——它把第 2–8 步完整做了一遍且可重复执行，`accept.sh` 再做服务器侧验收。

> 下文与两个脚本都假定安装目录是 `/opt/dsh-backend`（脚本里写死了 `APP_DIR`）。换路径要同时改脚本。

### 1. 前置
```bash
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv curl openssl iproute2 git ca-certificates
python3.11 -V                        # 期望 Python 3.11.x
sudo ufw allow 58443/tcp             # 云主机请在安全组放行同一端口
```

### 2. 服务账号与目录
中继要终结 TLS 并解析不受信任的输入，因此不使用 root。
```bash
sudo useradd --system --home-dir /opt/dsh-backend --shell /usr/sbin/nologin dshrelay
sudo mkdir -p /opt/dsh-backend && sudo chown dshrelay:dshrelay /opt/dsh-backend
```

### 3. 取代码与安装依赖
安装目录必须是空的，`git clone` 才写得进去；已有代码就改成 `rsync -a --delete ./ user@your-server:/opt/dsh-backend/`。
```bash
sudo -u dshrelay git clone https://github.com/AKHYui/DSH-Remote-backend.git /opt/dsh-backend
cd /opt/dsh-backend
sudo -u dshrelay mkdir -p var certs && sudo -u dshrelay chmod 700 var    # var 只有服务账号能进
sudo -u dshrelay python3.11 -m venv .venv
sudo -u dshrelay .venv/bin/python -m pip install -U pip setuptools
sudo -u dshrelay .venv/bin/pip install -e .              # 运行时依赖
sudo -u dshrelay .venv/bin/pip install -e ".[dev]"       # 还要跑测试时装这个
.venv/bin/python -V
```

### 4. 初始化数据库
```bash
sudo -u dshrelay .venv/bin/python -m app.cli init-db     # {"ok": true, "db": ".../var/relay.db"}
sudo chmod 600 /opt/dsh-backend/var/relay.db
```

### 5. 签发令牌
令牌**只在签发时显示一次**，库里只存 `SHA-256`；丢了重新签发即可。`issue-connector` 打印的片段可直接粘进插件配置（`serverUrl` / `connectorToken` / `deviceId`）。
```bash
sudo -u dshrelay .venv/bin/python -m app.cli issue-connector --name home-pc   # 给桌面插件
sudo -u dshrelay .venv/bin/python -m app.cli issue-device --name my-phone     # 给手机
sudo -u dshrelay .venv/bin/python -m app.cli pair-start --name "Pixel 8"      # 或走配对码
sudo -u dshrelay .venv/bin/python -m app.cli pair-approve 123456 --name "Pixel 8"
```

- 插件侧连接串形如 `serverUrl: wss://<your-relay-host>:58443/api/v1/attach`，`deviceId` 就是 `--name`。
- `revoke-device <deviceId>` / `revoke-connector <connectorId>` **立即生效**（前者下次调用 401，后者下次重连被拒，闭码 `4401`），手机与桌面互不影响。

### 6. TLS
证书放进 `certs/`：只要 `server.crt` + `server.key` 存在，`deploy.sh` 就会自动启用 HTTPS。
```bash
cd /opt/dsh-backend/certs
# 1) 内部 CA（自签，10 年）
sudo openssl req -x509 -newkey rsa:4096 -nodes -keyout ca.key -sha256 -days 3650 \
  -subj "/CN=DSH Relay Internal CA" -out ca.crt
# 2) 服务端证书：SAN 必须写你实际使用的地址
sudo openssl req -new -newkey rsa:2048 -nodes -keyout server.key -subj "/CN=dsh-relay" \
  -addext "subjectAltName=IP:203.0.113.10,IP:127.0.0.1,DNS:localhost" -out server.csr
sudo openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 3400 -sha256 -copy_extensions copy -out server.crt
sudo chmod 600 ca.key server.key && sudo chmod 644 ca.crt server.crt
```

- 客户端要信任 `ca.crt`：插件配 `tlsCaFile`，手机 App 把它打包进安装包。**`ca.key` 只留在这台服务器上**，不要进仓库、不要外发。
- 用公网证书（Let's Encrypt 等）时：**完整链**写进 `certs/server.crt`、私钥写进 `certs/server.key`，并且**不要**放 `certs/ca.crt`（否则 `deploy.sh` 会再拼一次链）。

### 7. systemd
**真正监听哪个地址取决于 `uvicorn --host/--port`**；下面是最小可用单元，`deploy.sh` 会写一份沙箱项更全的。
```ini
[Unit]
Description=DSH Remote Bridge relay
After=network-online.target
[Service]
Type=simple
User=dshrelay
WorkingDirectory=/opt/dsh-backend
Environment=DSH_RELAY_DB=/opt/dsh-backend/var/relay.db
ExecStart=/opt/dsh-backend/.venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 58443 --log-level info --no-server-header \
  --ws-max-size 8388608 --timeout-keep-alive 75 --ssl-certfile /opt/dsh-backend/certs/server-fullchain.crt --ssl-keyfile /opt/dsh-backend/certs/server.key
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/dsh-backend/var
CapabilityBoundingSet=
MemoryMax=768M
[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now dsh-relay
systemctl is-active dsh-relay        # active
```
别去掉 `--ws-max-size 8388608`（决定一次会话快照能否装进单帧）与 `--timeout-keep-alive 75`（要大于手机客户端 15 秒的连接复用时间）。手工起服务时用 `--ssl-certfile certs/server-fullchain.crt --ssl-keyfile certs/server.key`，fullchain 由 `deploy.sh` 生成。

### 8. 健康检查
```bash
curl -fsS --cacert /opt/dsh-backend/certs/ca.crt https://127.0.0.1:58443/healthz
# {"status":"ok","version":"0.1.0","protocol":1,"devices":{"total":1,"online":1},"phones":1,...}
sudo ss -ltnp | grep ':58443'
```
`devices.online` 是桌面是否在线，`phones` 是已配对的手机数。

### 9. 幂等部署与验收
以后更新代码或配置、重装依赖，都跑这两条（脚本按 root 写，用 `sudo`）：
```bash
sudo bash /opt/dsh-backend/deploy.sh     # 覆盖第 2–8 步，最后一行 deploy ok (https on port 58443)
sudo bash /opt/dsh-backend/accept.sh     # 服务器侧 11 组验收，最后一行形如 26 passed, 0 failed
```
- `deploy.sh` 可重复执行；用 `PORT=58443 HOST=0.0.0.0 MIRROR=<pypi 镜像>` 覆盖默认值；没有证书时大声警告并退回明文。
- `accept.sh` 检查只能从主机看到的东西：systemd 状态与沙箱、TLS 版本下限、`server.key` 权限、`/docs` 是否 404、访问日志脱敏、吊销是否即时、审计是否只有摘要、`relay.db`/`var/` 权限、本机 pytest、链路是否抖动。

### 10. 看日志
```bash
journalctl -u dsh-relay -n 80 --no-pager      # 最近 80 行；-f 跟着看
journalctl -u dsh-relay --since '10 min ago' | grep attach    # 只看插件接入
```
需要逐条看插件推来的事件时（事件很密，平时别开）：把 `DSH_RELAY_DEBUG_EVENTS` 设为 `1` 后 `systemctl restart dsh-relay`，看完再设回空值重启。

## 配置

全部是环境变量，都有默认值；systemd 单元里用 `Environment=` 设置。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DSH_RELAY_DB` | `backend/var/relay.db` | SQLite 路径（目录 0700、文件 0600） |
| `DSH_RELAY_HOST` | `127.0.0.1` | 预留：**监听地址由 `uvicorn --host` 决定** |
| `DSH_RELAY_PORT` | `8787` | 仅用于 CLI 打印的连接提示；**实际端口由 `uvicorn --port` 决定** |
| `DSH_RELAY_PUBLIC_URL` | 空 | 预留，当前不参与任何行为 |
| `DSH_RELAY_LOG_LEVEL` | `info` | 预留；实际级别由 `uvicorn --log-level` 决定 |
| `DSH_RELAY_PAIR_TTL_SECONDS` | `300` | 配对码有效期（秒） |
| `DSH_RELAY_PAIR_ATTEMPTS_PER_HOUR` | `20` | 配对接口限速（滑动窗口/小时） |
| `DSH_RELAY_OP_REQUESTS_PER_MINUTE` | `240` | op 与流接口限速（滑动窗口/分钟） |
| `DSH_RELAY_REQUEST_TIMEOUT_SECONDS` | `60` | 单次 op 转发到桌面的超时 |
| `DSH_RELAY_STREAM_IDLE_TIMEOUT_SECONDS` | `600` | 预留，当前不参与任何行为 |
| `DSH_RELAY_APPROVAL_TTL_SECONDS` | `120` | 审批/提问在手机上等待作答的上限 |
| `DSH_RELAY_HEARTBEAT_SECONDS` | `30` | 事件 WebSocket 心跳间隔（最小 5） |
| `DSH_RELAY_MAX_PENDING_PER_DEVICE` | `64` | 每台桌面同时在飞的请求上限 |
| `DSH_RELAY_EVENT_QUEUE_SIZE` | `512` | 单个流通道的缓冲帧数 |
| `DSH_RELAY_MAX_REQUEST_BYTES` | `4194304` | 请求体上限（4 MiB），超出按 413 拒绝 |
| `DSH_RELAY_ENABLE_DOCS` | `false` | 打开 `/docs` 与 `/openapi.json`（默认 404） |
| `DSH_RELAY_REDACT_ACCESS_LOG` | `true` | 访问日志里 `token=` 替换成 `<redacted>` |
| `DSH_RELAY_TRUST_PROXY_HEADERS` | `false` | 在可信反向代理之后才打开，用于取真实客户端 IP |
| `DSH_RELAY_DEBUG_EVENTS` | `false` | 逐条打印插件推来的事件（排错用） |

> 标「预留」的键会被正确解析但当前不驱动行为——列出来是怕你 grep 到变量名后误以为它生效。

CLI 子命令（`python -m app.cli <子命令>`，可加 `--db <path>` 覆盖数据库路径）：

| 子命令 | 说明 |
|---|---|
| `init-db` | 建表并退出（其余命令也会自动建表） |
| `issue-connector --name home-pc` / `issue-device --name my-phone` | 签发桌面插件令牌 / 手机令牌 |
| `pair-start --name "Pixel 8"` / `pair-approve <code> [--name] [--by]` | 生成 / 批准配对码 |
| `devices` / `connectors` / `desktops` | 列出已配对手机 / 连接器 / 拨入过的 DSH 主机 |
| `pending` | 列出尚未被认领的配对码 |
| `revoke-device <deviceId>` / `revoke-connector <connectorId>` | 立即吊销（软删除，审计保留） |
| `remove-desktop <id…> [--simulators] [--dry-run]` | 忘掉桌面行，见下节 |
| `audit [--limit 50]` | 查看最近若干条审计行 |

## 运维与排错

| 现象 | 处理 |
|---|---|
| 手机连不上：证书错误 | 客户端要信任签发服务端证书的 CA：插件配 `tlsCaFile`，App 内置 `ca.crt`。核对指纹 `openssl x509 -in certs/ca.crt -noout -fingerprint -sha256` |
| 手机连不上：拒绝 / 超时 | 先在本机 `curl -fsS --cacert certs/ca.crt https://127.0.0.1:58443/healthz`；本机通、外面不通 = 安全组 / `ufw` / 反向代理 |
| 插件连不上，闭码 `4401` | 连接器令牌无效或已被吊销：重新 `issue-connector` 并更新插件配置 |
| 插件连不上，闭码 `4001` | 同一 `deviceId` 被另一台机器抢占：每台桌面用唯一 `deviceId` |
| 插件连不上，闭码 `4400` | 协议版本不符或 `deviceId` 为空：两端用同一版本 |
| 插件报明文被拒 | 插件默认拒绝 `ws://`：部署 TLS，或仅在本地开发打开插件的 `allowInsecure` |
| 访问日志里的令牌 | 应显示 `token=<redacted>`；若是明文，检查 `DSH_RELAY_REDACT_ACCESS_LOG` 是否被改成 `false` |
| `/docs` 返回 404 | 正常；临时打开 `DSH_RELAY_ENABLE_DOCS=1` 并重启才能看接口文档 |
| 返回 413 / 429 / 504 | 请求体超过 4 MiB / 触发限速 / 桌面在超时内没答；见 `MAX_REQUEST_BYTES`、`OP_REQUESTS_PER_MINUTE`、`REQUEST_TIMEOUT_SECONDS` |
| 设备列表里多出 `remote-sim` 之类的离线设备 | 验收模拟器留在 `desktops` 表里的行：`... -m app.cli desktops` 查看，`remove-desktop --simulators --dry-run` 预演后去掉 `--dry-run` 执行 |

**安全边界**：把它跑在公网上，等于把持有手机令牌的人直接送到那台 DSH 面前（DSH 能在桌面执行命令）。因此传输必须是 TLS、令牌只存哈希且可单独吊销、审批永不自动批准、`/docs` 默认关闭。

## 测试
```bash
cd /opt/dsh-backend
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q                    # 98 个用例
DSH_PLUGIN_DIR=/path/to/DSH-Remote-plugin .venv/bin/python tools/e2e_smoke.py   # 跨语言端到端
```
`e2e_smoke.py` 用真实插件驱动真实中继，需要插件检出（默认找 `../plugin`）。`pytest` 配置在 `pyproject.toml`。Windows 上把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。

## 仓库结构

```
app/                 中继本体：配置、协议常量、存储、鉴权、中继核心、HTTP/WS 接口、CLI
tests/               98 个用例（含与插件 op 白名单逐字比对的跨语言测试）
tools/e2e_smoke.py   端到端冒烟：本地中继 + 真实插件
docs/PROTOCOL.md     线协议、HTTP/事件接口与 op 白名单（与插件仓库保持同步）
deploy.sh / accept.sh  幂等部署（第 2–8 步）/ 服务器侧 11 组验收
pyproject.toml       依赖与 Python 版本、pytest 配置；conftest.py 是测试夹具
```

## 相关仓库

- 桌面插件：[AKHYui/DSH-Remote-plugin](https://github.com/AKHYui/DSH-Remote-plugin)
- 中继后端：本仓库 [AKHYui/DSH-Remote-backend](https://github.com/AKHYui/DSH-Remote-backend)
- 手机 App：[AKHYui/DSH-Remote-app](https://github.com/AKHYui/DSH-Remote-app)

## 许可

MIT，见 [LICENSE](LICENSE)。
