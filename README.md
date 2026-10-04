# DSH Remote Bridge 中继（backend）

> 三个仓库之一：[桌面插件](https://github.com/AKHYui/DSH-Remote-plugin) · **中继后端（本仓库）** · [手机 App](https://github.com/AKHYui/DSH-Remote-app)

本目录可以独立成一个仓库部署：一个 FastAPI + SQLite 的中继服务。桌面插件**主动**出站连过来
（WSS），手机通过 HTTPS + WSS 对它下指令。它是一条哑管道——不解析、不落盘任何会话内容或模型
输出，只做鉴权、限速、转发与审计。

---

## 它是什么

```
Flutter App ──HTTPS/WSS──▶ 中继 ◀──出站 WSS── 桌面插件
```

中继是唯一有公网入口的一侧。桌面插件用连接器令牌建立一条长连接并声明自己的 deviceId、
能力（`ops` / `events` / `approvals`）；手机带设备令牌走 REST 调 op、走 SSE 拉流、走事件
WebSocket 订阅插件推送。中继不产生内容：它只把手机的请求转成 `req` 帧、把插件的 `res` /
`stream` / `evt` 帧转回请求方，并为每次 op 记一行审计（**只记参数的摘要**）。

**信任模型一句话**：拿到手机设备令牌的人就能驱动与之配对的桌面，因此 TLS + 每设备独立令牌 +
可即时吊销就是全部的安全性来源——没有第二道防线，也不该假装有。

中继自己存的东西只有两类：**令牌的 SHA-256 哈希**，以及**每次 op 的参数摘要（16 位十六进制，
SHA-256 前 16 位）**。提示词、文件路径、文件内容都只经过它、不留痕。

---

## 快速开始（本机）

需要 Python **≥ 3.11**（`pyproject.toml` 的 `requires-python`）。

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"      # Windows
```

Linux / macOS 下同样的三步，路径换成 `.venv/bin/python`：

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

`[dev]` 装的是 `httpx`、`pytest`、`websockets`（测试要用）；只跑服务本身用 `pip install -e .`
即可。如果机器上有 `uv`，等价写法是 `.venv\Scripts\python.exe -m pip` 换成
`uv pip install --python .venv\Scripts\python.exe -e ".[dev]"`；不装 `uv` 也完全没问题。
离线或网络慢时可加镜像，例如
`-i https://pypi.tuna.tsinghua.edu.cn/simple`。

启动：

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

零配置即可运行：数据库、日志、所有超时都有可用默认值。探活：

```powershell
curl.exe http://127.0.0.1:8787/healthz
```

```json
{"status":"ok","version":"0.1.0","protocol":1,"uptimeSeconds":0,"devices":{"total":0,"online":0},"phones":0,"pendingAsks":0,"phoneConnections":0}
```

各字段含义见「[调试](#调试)」。刚起服务时 `devices` 全 0、`phones` 为 0 是正常的——
还没人连过，这时 `/api/v1/devices` 会返回 401。

自检：

```powershell
.venv\Scripts\python.exe -m pytest              # 91 个用例
.venv\Scripts\python.exe tools\e2e_smoke.py     # 跨语言端到端，需要插件检出（见下）
```

`tools/e2e_smoke.py` 会启动真实中继并拉起**真正的 Node 插件**（用桩 DSH 上下文），因此手边必须
有一份插件源码，并通过 `DSH_PLUGIN_DIR` 指向它；没有该变量时脚本默认去找与 backend 并列的
`plugin/` 目录。用法与覆盖范围见「[调试](#调试)」。

---

## 配置

全部通过 `DSH_RELAY_*` 环境变量读取（`app/config.py`）。未设置或设为空字符串时一律走默认值。

| 变量 | 默认 | 含义 |
|---|---|---|
| `DSH_RELAY_DB` | `var/relay.db`（本目录下，与 `app/` 并列） | SQLite 路径；父目录会自动创建 |
| `DSH_RELAY_HOST` | `127.0.0.1` | 只用于 CLI 打印连接示例；实际监听地址由 uvicorn 参数决定 |
| `DSH_RELAY_PORT` | `8787` | 同上，只影响 CLI 打印的示例 URL |
| `DSH_RELAY_PUBLIC_URL` | 空 | 对外地址，预留给部署记录/文档；代码中不参与路由 |
| `DSH_RELAY_LOG_LEVEL` | `info` | 日志级别；只在进程内读取，uvicorn 实际使用 `--log-level` |
| `DSH_RELAY_PAIR_TTL_SECONDS` | `300` | 配对码有效期 |
| `DSH_RELAY_REQUEST_TIMEOUT_SECONDS` | `60.0` | 一元 op 等待插件回 `res` 的上限，超时 → `timeout` / HTTP 504 |
| `DSH_RELAY_STREAM_IDLE_TIMEOUT_SECONDS` | `600.0` | 预留配置：当前版本未对空闲流做强制回收，流靠插件 `end` 或手机断开结束 |
| `DSH_RELAY_APPROVAL_TTL_SECONDS` | `120` | 中继记住一条审批/提问的时间；过期即从可重连列表消失 |
| `DSH_RELAY_MAX_PENDING_PER_DEVICE` | `64` | 单桌面在途请求上限；超出 → `device_busy`（HTTP 429） |
| `DSH_RELAY_EVENT_QUEUE_SIZE` | `512` | 每条手机事件连接的队列长度；满了**丢新帧**而不是阻塞插件连接 |
| `DSH_RELAY_HEARTBEAT_SECONDS` | `30` | 向插件发 `ping` 的间隔，代码内下限 5 秒；静默超过 2.5 倍间隔即发 `bye` 并关链 |
| `DSH_RELAY_PAIR_ATTEMPTS_PER_HOUR` | `20` | 配对接口限速，按来源 IP；成功认领后清零 |
| `DSH_RELAY_OP_REQUESTS_PER_MINUTE` | `240` | `/op` 与 `/stream` 限速，按手机设备 ID（不是 IP） |
| `DSH_RELAY_TRUST_PROXY_HEADERS` | `false` | 是否用 `X-Forwarded-For` 作为限速归属 |
| `DSH_RELAY_ENABLE_DOCS` | `false` | 是否注册 `/docs` 与 `/openapi.json`（默认关闭：公网不发布整份接口拓扑） |
| `DSH_RELAY_MAX_REQUEST_BYTES` | `4194304`（4 MiB） | `POST/PUT/PATCH` 的 `Content-Length` 上限，超出直接 413 |
| `DSH_RELAY_REDACT_ACCESS_LOG` | `true` | 把访问日志中的 `token=...` 替换为 `token=<redacted>` |
| `DSH_RELAY_DEBUG_EVENTS` | `false` | 打印每条转发的订阅与每条插件事件，用于定位「事件到底有没有到中继」 |

校验行为：`*_SECONDS`、`*_BYTES`、`*_PER_*` 等数值变量在启动时解析，写错会抛出
`DSH_RELAY_XXX must be an integer/number` 并直接拒绝启动；布尔变量只有
`1 / true / yes / on`（忽略大小写）算真。`HOST` / `PORT` 不参与监听决策——uvicorn 的命令行参数
优先，别指望改环境变量能换端口。

**生产环境必须改的**：

- `DSH_RELAY_DB` → 固定到绝对路径（如 `/opt/dsh-relay/var/relay.db`），并保证只有服务账户可写；
- `DSH_RELAY_HOST` / `DSH_RELAY_PORT` → 让 CLI 打印的示例 URL 正确（监听地址仍写在 unit 里）；
- `DSH_RELAY_LOG_LEVEL` 与 uvicorn 的 `--log-level` → 至少 `info`，排障时 `debug`；
- `DSH_RELAY_MAX_REQUEST_BYTES` → 结合附件大小上调（见「[排错](#排错)」），但别设成无上限；
- `DSH_RELAY_REDACT_ACCESS_LOG` → **保持 `true`**。插件只能把连接器令牌放在查询串里（Node 的
  WHATWG `WebSocket` 构造函数不能设置请求头），uvicorn 会把请求行写进日志，于是每次重连都会
  把一个有效凭据写进 journal。关掉它等于把凭据落到磁盘上。

### 为什么必须脱敏访问日志

`RedactTokens` 过滤器同时挂在 `uvicorn.access` 与 `uvicorn.error` 上：HTTP 访问行来自前者，
WebSocket 的 `[accepted]` 行来自后者，只挂一个就会漏（`tests/test_api.py` 里有专门防回归的用例）。

---

## 部署（一台小服务器）

下面是一条完整路径：单机、uvicorn 自己终结 TLS、systemd 守护，全部手写、不依赖任何外部脚本。
若你另有自动化部署的脚本，它做的也只是把下面这些步骤重复一遍，遇到问题时仍然要回到这里逐项核对。

> 本仓库自带两个脚本，做的是同一件事，只是**幂等**：
> [`deploy.sh`](deploy.sh)（venv、依赖、数据库、TLS、非 root 服务账号、systemd 沙箱与资源上限、健康检查）
> 与 [`accept.sh`](accept.sh)（服务器侧验收：systemd、端口、令牌撤销、审计卫生、文件权限、测试套件）。
> 它们就在仓库根目录，`rsync`/`git clone` 过去即可用；本文剩下的部分则是它们的逐项解释，
> 出问题时按这里核对。

### 1. 账户、目录、数据库

```bash
sudo useradd --system --home-dir /opt/dsh-relay --shell /usr/sbin/nologin dshrelay
sudo mkdir -p /opt/dsh-relay/var
sudo git clone <本仓库地址> /opt/dsh-relay       # 或用 rsync 把本目录同步过去
cd /opt/dsh-relay                                # 仓库根目录就是本目录
sudo -u dshrelay python3 -m venv .venv           # 缺 venv 模块时先装 python3-venv
sudo -u dshrelay .venv/bin/python -m pip install -e .
sudo -u dshrelay .venv/bin/python -m app.cli init-db
```

数据库的权限预期：**`var/` 用 0700，数据库用 0600**。服务本身以 `UMask=0077` 运行，因此它
自己新建的文件天然是 0600；但如果你用 root 跑过 `init-db`，root 创建的 `relay.db` 需要手动
归位：

```bash
sudo chown -R dshrelay:dshrelay /opt/dsh-relay
sudo chmod 700 /opt/dsh-relay/var
sudo chmod 600 /opt/dsh-relay/var/relay.db
```

之后所有管理员命令都用 `sudo -u dshrelay` 执行，避免 root 与 `dshrelay` 两个身份交替创建
WAL / SHM 文件导致权限错乱。

### 2. 令牌（手机设备令牌 + 插件连接器令牌）

**没有 HTTP 管理接口**——签发、审批、吊销都需要服务器 shell 权限（`app/cli.py`）。库里只存
SHA-256 哈希，令牌**只显示一次**，请当场存好。

```bash
cd /opt/dsh-relay
py="sudo -u dshrelay .venv/bin/python"

# 桌面插件：一个 DSH 主机一个连接器
$py -m app.cli issue-connector --name home-pc
#   → {"connectorId": "con-…", "name": "home-pc", "token": "<43 字符的 URL-safe 密钥>"}

# 手机：直接签发设备令牌（不经过配对流程）
$py -m app.cli issue-device --name "Pixel 8"
#   → {"deviceId": "dev-…", "name": "Pixel 8", "deviceToken": "<同样的格式>"}
```

也可以走配对流程，由管理员在服务器上批准：

| 命令 | 作用 |
|---|---|
| `$py -m app.cli pair-start --name "Pixel 8"` | 生成 6 位配对码，默认 300 秒有效 |
| `$py -m app.cli pair-approve 123456 --name "Pixel 8"` | 批准某个码；未批准前手机认领会得到 `400 pair_not_ready` |
| `$py -m app.cli pending` | 列出还没被认领的配对码 |
| `$py -m app.cli devices` | 列出已配对的手机 |
| `$py -m app.cli connectors` | 列出桌面连接器 |
| `$py -m app.cli desktops` | 列出**拨进来过的 DSH 主机**（手机「选择设备」读的就是这张表） |
| `$py -m app.cli remove-desktop <id…>` | 忘掉某些主机行（比如验收脚本留下的模拟器） |
| `$py -m app.cli audit --limit 50` | 看最近若干条审计行 |

所有子命令都支持 `--db <path>` 覆盖数据库路径（默认取 `DSH_RELAY_DB`）。

### 幽灵设备：为什么会有，怎么清

验收脚本（`scripts/verify_remote.py`）每次都会以 `remote-sim` / `remote-events` / `remote-ask` /
`remote-defer` 的身份拨进来，于是 `desktops` 表里就多几行——而**那张表正是手机「选择设备」列的东西**，
所以手机上会出现几个永远离线的「设备」，看不出是测试数据。清理：

```bash
$py -m app.cli desktops                          # 先看
$py -m app.cli remove-desktop --simulators --dry-run   # 再看会删谁
$py -m app.cli remove-desktop --simulators       # platform == "simulator" 的全删
```

这里是**硬删除**而不是吊销：留一行墓碑，它就会以一个「离线设备」的样子继续出现在手机里，而那和一台
真的关机了的电脑长得一模一样。验收脚本现在跑完会自己删（需要 `DSH_DEPLOY_*` 环境变量，否则打印出上面
那条手敲命令）；`--keep-desktops` 可以跳过，方便本地拿模拟器当桌面用。

插件侧把令牌配成 `connectorToken`，服务地址配成
`wss://<你的域名或 IP>:<端口>/api/v1/attach`。连接器令牌进库时就是
`sha256(token)`，`store.verify_connector` 只按哈希查表。

**吊销**（立即生效，无需重启；吊销后该令牌的 REST 调用立刻 401、已建立的连接在下一次
鉴权/重连时被拒）：

```bash
$py -m app.cli revoke-device    dev-xxxxxxxxxxxx     # 手机丢了/被偷了
$py -m app.cli revoke-connector con-xxxxxxxxxxxx     # 换机器/怀疑泄漏
```

吊销是软删除（写 `revoked_at`），审计行与历史都保留。若某个令牌曾经进过仓库或日志，
**吊销并重新签发**是唯一正确的处置。

### 3. TLS：自建内部 CA

手机与插件都必须用 `https://` / `wss://`。中继自己终结 TLS 时，uvicorn 需要一个包含**服务端
证书 + CA 证书**的链文件（客户端才能自己补齐链）和私钥：

```bash
cd /opt/dsh-relay
mkdir -p certs && chmod 700 certs

# --- 只做一次：内部 CA（私钥只存在于部署机，永远不要提交进任何仓库）---
openssl genrsa -out certs/ca.key 4096
chmod 600 certs/ca.key
openssl req -x509 -new -key certs/ca.key -sha256 -days 3650 \
  -subj "/CN=dsh-relay internal CA" -out certs/ca.crt

# --- 每个中继主机一份服务端证书（SAN 必须包含手机实际使用的地址）---
openssl genrsa -out certs/server.key 2048
chmod 600 certs/server.key
openssl req -new -key certs/server.key -subj "/CN=relay.example.com" -out certs/server.csr
cat > certs/server.ext <<'EOF'
subjectAltName = DNS:relay.example.com, IP:203.0.113.10
extendedKeyUsage = serverAuth
EOF
openssl x509 -req -in certs/server.csr -CA certs/ca.crt -CAkey certs/ca.key -CAcreateserial \
  -days 825 -sha256 -extfile certs/server.ext -out certs/server.crt

cat certs/server.crt certs/ca.crt > certs/server-fullchain.crt   # uvicorn 用这一份
chmod 600 certs/server.key
```

`certs/` 目录本身要让服务账户能进去。私钥不能 world-readable，所以给服务用户单独读权限，或者
直接把私钥交给它（中继要自己终结 TLS，它必须能读）：

```bash
sudo chown -R root:dshrelay /opt/dsh-relay/certs
sudo chmod 750 /opt/dsh-relay/certs
sudo chmod 640 /opt/dsh-relay/certs/server-fullchain.crt
sudo chmod 640 /opt/dsh-relay/certs/ca.crt
sudo chown dshrelay:dshrelay /opt/dsh-relay/certs/server.key   # 或者 root:dshrelay + 640
sudo chmod 600 /opt/dsh-relay/certs/server.key
sudo rm -f /opt/dsh-relay/certs/server.csr
```

**为什么手机要内置 CA，而不是依赖系统信任库**：Android 7（API 24）起，App 默认不信任用户
安装的 CA，只认系统证书库；而把 CA 装进系统库需要 root。因此手机端必须把 `ca.crt` 作为
资源打进应用，校验时以它为准（或在 `network_security_config.xml` 中显式声明）。后果是：
换 CA 就等于换客户端版本，所以 `certs/ca.key` 要长期保管好，别丢、别提交。

插件侧（Node）同样需要信任这张 CA，可把 `ca.crt` 路径配给它，或用系统信任库。

### 4. systemd unit

`/etc/systemd/system/dsh-relay.service`：

```ini
[Unit]
Description=DSH Remote Bridge relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dshrelay
Group=dshrelay
WorkingDirectory=/opt/dsh-relay
Environment=DSH_RELAY_DB=/opt/dsh-relay/var/relay.db
Environment=DSH_RELAY_HOST=0.0.0.0
Environment=DSH_RELAY_PORT=8787
Environment=DSH_RELAY_LOG_LEVEL=info
Environment=PYTHONDONTWRITEBYTECODE=1

# --ws-max-size：单条 WebSocket 消息上限（字节）。协议里一次会话快照可能很大，
#   把上限放宽到 8 MiB；同时要确认插件的发送侧也允许这么大的帧。
# --timeout-keep-alive：远高于任何客户端的空闲超时。uvicorn 默认 5 秒，而移动端
#   会复用连接池里的连接（Dart 的 HttpClient 默认保留 15 秒），一旦复用了服务端
#   已经关掉的 socket，表现就是 "Connection closed before full header was received"
#   ——链路完全健康却报错。客户端仍然先超时，这里只是把余量拉大。
ExecStart=/opt/dsh-relay/.venv/bin/python -m uvicorn app.main:app \
          --host 0.0.0.0 --port 8787 \
          --log-level info \
          --ws-max-size 8388608 \
          --timeout-keep-alive 75 \
          --no-server-header \
          --ssl-certfile /opt/dsh-relay/certs/server-fullchain.crt \
          --ssl-keyfile /opt/dsh-relay/certs/server.key
Restart=always
RestartSec=3
KillSignal=SIGINT
TimeoutStopSec=15
UMask=0077
LimitNOFILE=8192

# --- sandboxing -----------------------------------------------------------
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
# ProtectSystem=strict 之下，只有数据库目录可写。
ReadWritePaths=/opt/dsh-relay/var
# 这台机器上还跑着别的服务：跑飞的中继被 OOM 杀掉重启，而不是拖垮整机。
MemoryMax=768M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dsh-relay
sudo systemctl status dsh-relay --no-pager
curl --cacert /opt/dsh-relay/certs/ca.crt https://127.0.0.1:8787/healthz
```

几点必须知道的：

- **单 worker**。请求关联表、订阅表、待审批列表和限速计数都在进程内存里。`--workers 2` 会让
  每个 worker 只掌握一部分状态，也会让限速形同虚设。systemd 里用 `Restart=always` 换可用性，
  不要用多 worker。
- **TLS 可以交给反向代理**。若你有 nginx / Caddy，删掉 `--ssl-certfile` / `--ssl-keyfile`，
  改听 `127.0.0.1:8787`，由代理终结 TLS，并且**必须转发 WebSocket 升级头**
  （`/api/v1/attach`、`/api/v1/events`），同时把 `/api/v1/stream` 的响应缓冲关掉
  （nginx：`proxy_buffering off;`），否则 SSE 会被整段缓冲。代理确实会覆盖
  `X-Forwarded-For` 时，才可以把 `DSH_RELAY_TRUST_PROXY_HEADERS=1` 打开，否则客户端能伪造
  IP 绕过限速。
- **暴露在公网上时保持 `DSH_RELAY_ENABLE_DOCS=false`**（默认）：不发布整份接口拓扑。

---

## 调试

按「先看健康、再看日志、最后查审计」的顺序走，绝大多数问题三步内能定位。

### 1. `healthz` 字段

```powershell
curl.exe -s http://127.0.0.1:8787/healthz
```

| 字段 | 含义 | 怎么读 |
|---|---|---|
| `status` | 固定 `ok` | 进程还活着，能回 HTTP |
| `version` | 服务版本 | 与部署的代码对比，确认重启过 |
| `protocol` | 线协议版本（当前 `1`） | 与插件必须一致，不一致时握手直接拒 |
| `uptimeSeconds` | 进程启动至今秒数 | 意外变小 = 服务刚重启过（或被 OOM 杀过） |
| `devices.total` | 注册过的桌面总数 | 历史累计，不随掉线减少 |
| `devices.online` | 当前有活跃链路的桌面数 | **total > 0 而 online = 0 ⇒ 插件没连上** |
| `phones` | 未吊销的手机设备数 | 数量对不上说明有令牌被吊销了 |
| `pendingAsks` | 中继记着的待决审批/提问数 | 长时间不降 = 手机没答，桌面在等 |
| `phoneConnections` | 当前连着的手机事件连接数 | 手机 App 在前台时应该 ≥ 1 |

### 2. 请求日志

```bash
journalctl -u dsh-relay -n 80 --no-pager
```

默认能看到 uvicorn 的访问行（`token=` 已被脱敏成 `<redacted>`）与中继自己的错误日志。
再加 `-f` 可以边操作边看。

中继不打印任何会话内容。唯一能看见「事件流量」的开关是：

```bash
# 临时：写进 systemd 管理器环境（重启后在 unit 里手动补上，或直接永久写进 [Service]）
sudo systemctl set-environment DSH_RELAY_DEBUG_EVENTS=1
sudo systemctl restart dsh-relay
journalctl -u dsh-relay -f | grep -E 'evt |sub '
```

永久打开就把 `Environment=DSH_RELAY_DEBUG_EVENTS=1` 写进 unit 的 `[Service]` 段再
`systemctl daemon-reload && systemctl restart dsh-relay`。

打开后会打印每条转发给插件的订阅（`sub <device> topics=… forwarded to plugin as …`）、每条
从插件收到的事件、以及它匹配到几个订阅者。**默认关闭**：会话事件很吵。它存在的理由是
「插件根本没推」和「中继把事件丢了」从手机那头看起来完全一样，而中继是唯一能不加重启就分辨
两者的地方。排完障记得关掉。

### 3. 审计表：这次 op 到底走没走中继

手机报错、桌面说没收到时，答案在 `audit` 表里。表结构（`app/store.py` 的 `SCHEMA`）：

| 列 | 含义 |
|---|---|
| `ts` | Unix 秒 |
| `device_id` | 目标桌面 ID |
| `op` | op 名，如 `session.prompt` |
| `args_digest` | 参数的稳定摘要，**SHA-256 前 16 位十六进制**；原文从不落盘 |
| `ok` | 1 / 0 |
| `ms` | 中继侧耗时（毫秒） |
| `error_code` | 失败时的协议错误码，如 `timeout`、`device_offline` |

两条索引：`idx_audit_ts`、`idx_audit_device`。查询不必依赖 `sqlite3` 命令行工具，用 venv 里的
Python 只读打开即可：

```bash
cd /opt/dsh-relay && sudo -u dshrelay .venv/bin/python - <<'PY'
import sqlite3
conn = sqlite3.connect("file:var/relay.db?mode=ro", uri=True)
for row in conn.execute(
    "SELECT ts, device_id, op, args_digest, ok, ms, error_code FROM audit ORDER BY ts DESC LIMIT 20"
):
    print(row)
PY
```

也可以直接用 CLI 看最近若干行：`sudo -u dshrelay .venv/bin/python -m app.cli audit --limit 50`。
输出是每行一个元组的列表（只有失败的行才有 `error_code`），例如：

```
(1791035705, 'dev-sim', 'fileUploads.upload', '3ba9602f2221db61', 1, 100, None)
(1791035705, 'dev-sim', 'session.follow',     'e7f035077c19ec9c', 1,  11, None)
(1791035704, 'dev-sim', 'session.list',       '44136fa355b3678a', 1, 156, None)
```

怎么读：**有行且 `ok=1`** ⇒ 中继确实把请求转给了插件并收到了成功响应，问题在插件之后
（DSH 内部）。**有行但 `ok=0`** ⇒ 看 `error_code` 对症下药。**完全没有行** ⇒ 请求根本没走到
中继的转发逻辑（被 401 挡在鉴权、被 429 限流、被 413 拒在中间件，或手机压根没发出去）。

### 4. 跨语言端到端冒烟

```powershell
$env:DSH_PLUGIN_DIR = "D:\path\to\plugin"   # 指向插件检出目录
.venv\Scripts\python.exe tools\e2e_smoke.py
```

它起一个真实中继，用**真正的 Node 插件**（插件仓库里的模拟器入口，只把 DSH 上下文换成桩）当
客户端，因此必须有一份插件源码，靠 `DSH_PLUGIN_DIR` 指定；未设置时默认找与 backend 并列的
`plugin/`。
退出码 0 = 全部通过，1 = 有失败项。当前跑下来是 **34/34** 项，覆盖：

- 握手与设备注册（在线状态、`platform`、能力集、`healthz` 计数）；
- 一元 op（`session.list`、本地由插件处理的 `harness.info`、`session.prompt` 真的到达插件）；
- 白名单外的 op 被拒（`501`）；
- 附件上传往返：1 MiB 原始字节的 base64 穿过 HTTP body 上限、插件 WebSocket 帧与网关参数校验，
  校验文件长度与文件名都没被截断；
- SSE 流（`session.follow`）：`event: open` 与 `event: chunk`；
- 事件扇出：订阅被 ack，事件带 `sessionId` 与单调 `seq` 到达手机；
- 审批往返：手机决议被接受、插件按「允许一次」执行，**并且断言竞争失败的一方——桌面上的原生
  提示被撤回**；同一条 ask 第二次提交返回 404；
- 回落路径：手机不答时，由桌面自己的应答器处理；
- 审计：op 有行，且每行 `args_digest` 都是 16 位。

### 5. 单元 / 集成测试

```powershell
.venv\Scripts\python.exe -m pytest -q      # 91 passed
```

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_protocol.py` | 14 | 帧编解码、未知帧不致命、op 白名单与 JS 侧一致、topic 常量 |
| `tests/test_store.py` | 11 | 令牌哈希与吊销、配对（需管理员批准）、审计只留摘要 |
| `tests/test_relay.py` | 28 | 请求关联/超时/取消、流与背压丢弃、订阅与扇出、审批登记与过期 |
| `tests/test_api.py` | 23 | REST 状态码与错误信封、413、429、401、日志脱敏、配对往返 |
| `tests/test_e2e.py` | 15 | 真起 uvicorn：握手、替换旧连接、心跳、SSE、事件、审批 |

`tests/test_protocol.py::test_python_and_plugin_op_allowlists_agree` 会去读插件的
`src/protocol.js`，把两边的 op 表逐项对比。只部署本目录时该文件不存在，用例会 **skip** 而不是
失败——这是有意为之：跨语言一致性只有在两半都检出时才能验。

---

## HTTP / 事件接口

所有 REST 都需要 `Authorization: Bearer <设备令牌>`；WebSocket 额外接受 `?token=`（浏览器与
Node 的 `WebSocket` 构造函数无法设置请求头）。响应统一是
`{"ok": true, "value": …}` 或 `{"ok": false, "error": {"code": …, "message": …}}`。

| 方法与路径 | 用途 |
|---|---|
| `GET /` | 服务描述：`service`、`version`、`protocol`、`health` |
| `GET /healthz` | 存活探针，**无需鉴权**（见上表） |
| `POST /api/v1/auth/pair/start` | 手机发起配对，返回 6 位码；按 IP 限速 |
| `POST /api/v1/auth/pair/claim` | 用已批准的码换设备令牌；成功即消耗该码 |
| `GET /api/v1/devices` | 桌面清单：`id/name/online/platform/harnessVersion/capabilities/lastSeen/pendingRequests/connectedAt/linkAgeSeconds` |
| `GET /api/v1/devices/{device_id}/sessions` | `session.list` 的便捷路由 |
| `POST /api/v1/devices/{device_id}/op` | 一元 op：`{op, args}`，等插件的 `res` |
| `POST /api/v1/devices/{device_id}/stream` | 流式 op（当前仅 `session.follow`），返回 SSE：`event: open` / `chunk` / `end` / `error` |
| `GET /api/v1/approvals` | 仍在等待的审批/提问（可带 `?deviceId=`）；手机重连后靠它补看漏掉的请求 |
| `POST /api/v1/approvals/{ask_id}` | 递交决议 `{decision: approved\|denied\|cancelled, answers?}`；单次有效 |
| `WS /api/v1/events` | 手机事件通道：发 `{t:"sub", id, deviceId, topics[], args}` / `{t:"unsub", id}` / `{t:"ping", id}`，收 `ready` / `ack` / `error` / `evt` |
| `WS /api/v1/attach` | 插件接入通道：连接器令牌鉴权，首帧必须是 `hello`，之后 `req`/`cancel`/`sub`/`unsub`/`approval`/`ping` |

关键状态码：

| 码 | 含义 |
|---|---|
| `401` | `unauthorized`：设备令牌缺失/错误/已吊销 |
| `403` | `session_not_allowed`：会话不在允许范围内 |
| `404` | `unknown_device`（这台桌面从未连过）或 `unknown_ask`（审批未知/过期/设备离线） |
| `409` | `cancelled`：调用被取消 |
| `413` | `payload_too_large`：body 超过 `DSH_RELAY_MAX_REQUEST_BYTES` |
| `429` | `rate_limited`（配对或 op 限速）或 `device_busy`（在途请求超过 `MAX_PENDING_PER_DEVICE`） |
| `501` | `op_not_supported`：中继白名单不接受该 op，或该 op 不是流式却打到了 `/stream` |
| `503` | `device_offline` / `link_lost`：桌面不在线或链路刚断 |
| `504` | `timeout`：插件在 `REQUEST_TIMEOUT_SECONDS` 内没回 |

帧格式、字段与 close code（`4001` 被新连接替换、`4400` 协议错误、`4401` 未授权）见
[`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

---

## 排错

| 症状 | 原因 / 处理 |
|---|---|
| `413 payload_too_large`，`request body exceeds N bytes` | 请求体超过 `DSH_RELAY_MAX_REQUEST_BYTES`（默认 4 MiB）。**附件走的就是这条路**：文件以 base64 放在 op 参数里，base64 会把体积撑大约 4/3，1 MiB 原始数据 ≈ 1.4 MiB body。传更大的附件只有两条路——上调这个上限（同时确认 uvicorn 的 `--ws-max-size` 与插件发送侧也放得下），或改用别的方式传大文件。**别设成 0 或无穷大**：这台机器还跑着别的服务 |
| `429 rate_limited`，`too many requests; retry in Ns` | 按**手机设备**计的 `OP_REQUESTS_PER_MINUTE`（默认 240/分钟，`/op` 与 `/stream` 共用）。消息里带重试秒数。轮询式客户端应拉长间隔或改用事件订阅 |
| `429 rate_limited`，`too many pairing attempts` | 按 IP 计的 `PAIR_ATTEMPTS_PER_HOUR`（默认 20/小时）。成功认领会清零计数 |
| `503 device_offline` | 中继上这台桌面没有活跃链路。依次确认：桌面上的 DSH 与插件进程是否在跑（重启 DSH 试试）；插件配置里的 `serverUrl` 是否是 `wss://<中继>/api/v1/attach`；`connectorToken` 是否被吊销过。用 `journalctl -u dsh-relay -n 80` 看有没有 `4401`（令牌无效）或反复重连的握手记录；`/healthz` 的 `devices.total > 0` 而 `online = 0` 就是这个状态 |
| `501 op_not_supported`，消息是 `op 'xxx' is not allowed` | **中继**拒绝：该 op 不在 `app/protocol.py` 的 `ALLOWED_OPS` 白名单里，请求根本没到桌面。这是纵深防御 |
| 同样是 `501`，但消息是 `op xxx is not supported by this bridge` | **插件**拒绝：op 通过了中继白名单，但插件的 op 表里没有它。两句措辞的差别就是判断哪一层拒绝的依据 |
| `501 op_not_supported`，打的是 `/stream` | `session.follow` 之外的 op 必须走 `/op`；反过来 `session.follow` 打 `/op` 会得到 `400 bad_args` |
| `401 unauthorized` | 令牌缺失、拼错，或刚做过轮换/吊销。轮换后手机与插件都必须换用新令牌：旧令牌立即失效，正在跑的连接会在下次鉴权或重连时被拒 |
| 审批/提问一直没人能答 | 中继记住它的时间是 `APPROVAL_TTL_SECONDS`（默认 120 秒），超时即被剪掉，之后再提交决议一定是 `404 unknown_ask`。手机如果在后台错过推送，应调 `GET /api/v1/approvals` 重新列出（它是为这个场景准备的），或让插件放宽自己的等待时间 |
| 手机提示 `Connection closed before full header was received`，但链路看起来正常 | 复用了服务端已关闭的空闲连接。把 uvicorn 的 `--timeout-keep-alive` 抬到远高于客户端空闲超时（unit 示例里是 75 秒），并让客户端先超时；幂等的 GET 可以重试一次，POST 绝不 |
| 事件时有时无 | 打开 `DSH_RELAY_DEBUG_EVENTS=1`：日志里有 `sub` 但没有对应的 `evt` ⇒ 插件侧没推；有 `evt` 但 `matched 0 subscriber(s)` ⇒ 手机没订阅或订阅的 `deviceId`/topic 不对。另外每条手机连接的事件队列是 `EVENT_QUEUE_SIZE`（默认 512），满了**丢新帧**——这正是它设计的取舍：丢帧可恢复，阻塞插件连接不可恢复 |
| 中继放行、插件却说这个 op 不支持（或反之） | 两边的 op 白名单漂移了。它们是用两种语言手写的，必须同步维护；`tests/test_protocol.py::test_python_and_plugin_op_allowlists_agree` 就是为这个存在的（需要插件检出，否则 skip） |
| 服务莫名其妙重启过 | 看 `/healthz` 的 `uptimeSeconds` 与 `journalctl -u dsh-relay --no-pager \| grep -i -E 'oom\|killed'`。`MemoryMax` 之下的 OOM 会被 systemd 重启 |

---

## 安全清单

- **只走 TLS**：`https://` / `wss://`，不接受明文；生产环境别用自签但未内置 CA 的证书，
  那只会被客户端忽略掉。
- **每设备独立令牌**：一台手机一个设备令牌、一台桌面一个连接器令牌，绝不共用。库中**只存
  SHA-256 哈希**，明文只在签发那一刻出现一次。
- **可即时吊销**：`revoke-device` / `revoke-connector` 立即生效；怀疑泄漏时先吊销再排查。
- **审计只留摘要**：`args_digest` 是参数的 16 位十六进制摘要，提示词、路径、文件内容都不落盘。
- **请求体与限速**：`MAX_REQUEST_BYTES`（413）、`OP_REQUESTS_PER_MINUTE`（429）、
  `MAX_PENDING_PER_DEVICE`（`device_busy`）三层都别关。
- **默认不打印内容**：访问日志中的 `token=` 被脱敏；事件级日志需要显式打开
  `DSH_RELAY_DEBUG_EVENTS`，排障后关掉。
- **`ENABLE_DOCS` 保持关闭**：公网不发布完整接口拓扑。
- **CA 私钥永不入库**：`certs/ca.key` 只存在于部署机（`chmod 600`），永远不要提交进任何仓库。
  `.gitignore` 要覆盖私钥、`var/`、`.env`，以及任何保存签发结果（`.mint*.txt` 之类）的凭据文件。
- **单 worker**：限速计数与请求关联表都在进程内存里，多 worker 会同时削弱安全性与一致性。

---

## 许可

MIT。
