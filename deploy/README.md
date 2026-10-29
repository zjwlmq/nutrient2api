# Linux 服务器部署

使用 Docker Compose 运行 nutrient2api，由现有宝塔、1Panel 或 Nginx 提供域名和 HTTPS。
服务默认只发布到宿主机 `127.0.0.1:8787`。公网仅开放 HTTPS 和证书签发所需端口。
该部署适合受控的小规模使用；服务仍基于 Python 标准库 HTTP 服务器。

## 无密钥启动

现在可以先部署，进入网页后台后再添加 Key。没有 `.env` 或其中的密钥留空都可以启动。
首次部署：

```bash
git clone https://github.com/zjwlmq/nutrient2api.git
cd nutrient2api
docker compose up -d --build
docker compose logs --tail=100 nutrient2api
```

已克隆到 `/root/nutrient2api` 的服务器，先退出 nano（Ctrl+X），再执行：

```bash
cd /root/nutrient2api
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=100 nutrient2api
```

确认 `git pull` 成功后再构建。日志中会显示 `INITIAL SETUP CODE: ...`，保存冒号后面的初始化码。
这个码用于首次设置管理员密码，初始化完成后立即失效；它不是 Nutrient Key。
也可执行 `docker compose exec nutrient2api python -m nutrient2api setup-code` 查看尚未使用的初始化码。

按下文配置 HTTPS 反向代理后，打开 `https://你的域名/admin/`，输入初始化码，设置至少 12 个字符的管理员密码。
随后可以添加「主账户」「备用账户」等多个 Nutrient Key，也可以先留空。没有可用账户时，聊天和嵌入接口返回 503，后台仍可使用。

尚未配置域名时，可从自己电脑执行 SSH 转发，再访问电脑上的 `http://127.0.0.1:18787/admin/`：

```bash
ssh -L 18787:127.0.0.1:8787 root@你的服务器IP
```

电脑上的 `127.0.0.1` 默认指向电脑自身，只有建立上述 SSH 转发后才能通过它访问服务器。
不要将 `.env` 或数据目录放到网站静态目录。镜像只包含 Python 和前端源码，账户数据单独保存在 Docker 数据卷。
程序本身不读取 `.env`；Compose 读取其中可选的环境配置。

## 启动并检查

需要 Docker Engine 和 Docker Compose。进入项目目录执行：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 nutrient2api
curl --fail http://127.0.0.1:8787/health
```

健康检查只证明本地进程存活，不检查上游密钥、余额或模型权限。
如 8787 已被占用，在 `.env` 设置 `NUTRIENT2API_PUBLISH_PORT=其他空闲端口`，并同步修改反向代理目标。
修改端口等 `.env` 配置后执行 `docker compose up -d --force-recreate`；单独 restart 不会重新注入环境变量。
上游 Key 和客户端令牌在网页中保存后立即生效，无需重启。

## 面板和域名

1. 将域名的 DNS 记录指向服务器。
2. 在现有面板创建反向代理网站，绑定域名，申请并启用 HTTPS。
3. 若面板代理运行在宿主机上，目标填写 `http://127.0.0.1:8787`，代理整个站点（包括 `/admin/`），保留原路径、Host、Cookie 和 Authorization 请求头。
4. 关闭响应缓冲与缓存，设置上游读取空闲超时为 360 秒。保留请求缓冲，以便传给 Python 的请求带有 Content-Length。
5. 设置请求体上限 10 MiB、读取超时及访问频率限制。独立 Nginx 的完整模板见 [nginx.conf.example](nginx.conf.example)。

代理必须用实际协议覆盖 `X-Forwarded-Proto`（Nginx：`proxy_set_header X-Forwarded-Proto $scheme;`），
使 HTTPS 后台登录 Cookie 带上 Secure。不要为后台接口添加跨域允许头，不要缓存后台响应。
后台包含 HttpOnly/SameSite Cookie、CSRF 校验和登录频率限制；公网访问仍应使用 HTTPS。

Nginx 模板中的 `limit_req_zone` 和 `limit_conn_zone` 必须放在 `http` 级别。
若面板已经生成 `server` 配置，不要再粘贴一套重复的 server/listen/证书配置；将对应代理设置合入现有站点。
模板使用占位域名和证书路径，替换后先运行 `nginx -t`，成功后再重载。

**若面板的 OpenResty/Nginx 在容器内：**容器里的 `127.0.0.1` 指向该容器自身。
请确认它使用宿主网络；如果使用 bridge 网络，应把代理容器和本服务接入同一个 Docker 网络，
代理到 `http://nutrient2api:8787`。不要为了连通代理而将 8787 直接开放到公网。
确认面板容器的网络名称后，执行：

```bash
cp deploy/compose.proxy-network.yaml.example compose.override.yaml
```

在 `.env` 添加 `PROXY_NETWORK=代理容器现有的网络名称`，然后执行 `docker compose up -d`。
此时面板代理目标使用 `http://nutrient2api:8787`。仅将信任的容器接入该网络。

无面板时，在已安装 Nginx 并取得证书后，按模板配置独立站点。
Nginx 缓冲与超时行为见 [官方说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。

## 客户端配置与验收

在后台「账户管理」中添加每个账户的 Key。名称仅用于区分账户；Key 需要上游开通 AI Gateway 权限。
「检测」只读取上游模型列表，不发起生成，也不证明账户余额或每个模型可用。
保存后列表仅显示 Key 末四位；编辑时留空表示保留原 Key。

启用的账户按轮询调度。401/403 冷却 5 分钟，429 冷却 1 分钟，并在尚未输出首个事件时尝试其他账户。
网络超时、5xx 和已经开始的流式回复不自动重放。禁用或删除账户会阻止新的调度，不中断已在进行的请求。

客户端选择 Chat Completions 兼容接口：

- Base URL：`https://你的域名/v1`
- API Key：后台「连接你的客户端」中点击「复制令牌」得到的值
- 模型：例如 `gpt-6-luna`，或网关别名 `nutrient-s`

本服务没有提供 `/v1/responses` 路由。需要 Responses 接口的客户端不能仅靠更换 Base URL 接入。
模型列表来自本地映射，实际可用模型和额度以你的上游账号为准。

先验证不收费的本地接口。运行后输入访问令牌，避免将密钥直接写入命令历史：

```bash
read -rsp 'Proxy API token: ' PROXY_TOKEN
echo
curl --fail -H "Authorization: Bearer $PROXY_TOKEN" http://127.0.0.1:8787/v1/models
```

替换下列域名后执行一次流式聊天验收；该请求会实际调用上游并可能产生费用：

```bash
curl --fail-with-body -N https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"reply: ok"}],"stream":true,"max_completion_tokens":2000,"store":false}'
unset PROXY_TOKEN
```

验收应看到独立的 `data:` 事件及最后的 `data: [DONE]`。去掉 Authorization 请求 `/v1/models` 应返回 401。
再验证一次 `stream:false`；使用工具的客户端还应完成“工具调用 → 工具结果 → 最终回答”的完整回合。
`store:false` 会在兼容回退中保留，但不代表上游零数据留存。

## 运行和更新

```bash
docker compose logs -f --tail=100 nutrient2api
docker compose restart nutrient2api
```

账户、客户端令牌、管理员密码哈希及调用统计保存在 `nutrient2api_data` 命名数据卷，容器路径是 `/app/data/control.db`。
`git pull`、重建、重启和普通 `docker compose down` 会保留数据；**不要执行 `docker compose down -v`**，该命令会删除数据卷。
保持原项目目录和 Compose 项目名，避免意外创建一个空的新数据卷。

更新前备份数据库、`.env` 和旧镜像。可用 SQLite 在线备份，再复制到宿主机：

```bash
mkdir -p backups
chmod 700 backups
docker compose exec -T nutrient2api python -c "import sqlite3; src=sqlite3.connect('/app/data/control.db'); dst=sqlite3.connect('/app/data/backup.db'); src.backup(dst); dst.close(); src.close()"
docker compose cp nutrient2api:/app/data/backup.db "./backups/control-$(date +%F-%H%M%S).db"
chmod 600 backups/*.db
```

数据库包含明文上游 Key 和访问令牌，依靠目录权限保护，不是加密保险库。请保护备份，勿上传公开仓库。
管理员密码仅保存 PBKDF2 哈希。调用记录最多保留 200 条，记录账户、模型、状态和耗时，不记录提问或回复。
一次请求切换账户时，每次上游尝试分别计数；检测不计入调用统计。

从旧版升级时，已有 `.env` 中的 `NUTRIENT_API_KEY` 和 `NUTRIENT2API_TOKEN` 只在首次创建数据库时导入。
之后以网页保存的配置为准，修改 `.env` 不会覆盖这些值。首次没填 Key 也没关系，稍后直接在网页添加。
所有 API 请求现在都必须带客户端令牌，包括本机调用；初始化会自动生成令牌。

忘记管理员密码时执行：

```bash
docker compose exec nutrient2api python -m nutrient2api reset-admin
```

刷新 `/admin/`，用输出的新初始化码重新设置密码。账户、调用记录和客户端令牌会保留；旧登录会话失效。
正常登录有效期为 8 小时，服务重启后需要重新登录。
默认限制为 32 个活动连接、10 MiB 请求体、30 秒客户端套接字空闲超时，以及 256 MiB 容器内存。
容器日志轮转为 3 个 10 MiB 文件。大图片、长上下文或更高并发需要按实测调整请求体和内存限制。

遇到工具与推理参数冲突时，服务会调用 Responses 获取完整结果，再封装为 SSE；这条回退路径会推迟首个内容事件。
上游断流会作为 SSE error 返回，客户端应检查错误事件，不能只检查 HTTP 200。
仅明确的 401/403/429 拒绝会切换账户；生成请求遇到网络超时或 5xx 不自动重发。
Compose 的环境变量和容器限制配置见 [Docker 官方说明](https://docs.docker.com/reference/compose-file/services/)。

## 通过 GitHub 更新

项目仓库：[zjwlmq/nutrient2api](https://github.com/zjwlmq/nutrient2api)。
仓库公开，服务器克隆和拉取更新不需要 GitHub 登录。首次部署可执行：

```bash
git clone https://github.com/zjwlmq/nutrient2api.git
cd nutrient2api
```

然后按前文启动，在网页后台填写 Key。
若之前使用 ZIP 部署，应先备份 `.env`，将仓库克隆到新目录，再复制 `.env` 并按原端口和反向代理配置切换。

以后进入服务器上的仓库目录，执行：

```bash
git status --short
git pull --ff-only
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 nutrient2api
```

确认 `git pull` 成功后再执行后续构建命令；发生本地修改冲突时先保留修改并处理冲突。
`.env`、本机 `data/` 和数据库文件已加入 Git 忽略规则，正常拉取更新不会覆盖密钥。
服务器专用的网络配置可保存在 `compose.override.yaml`；建议将该文件加入服务器上的 `.git/info/exclude`，避免误提交。
面板中的站点和证书配置独立于此仓库。

## 本地回归

这些测试使用本地模拟上游，不需要 API 密钥，也不产生模型调用费用：

```bash
python -B tests/test_prompt.py
python -B tests/test_stream_scrub.py
python -B -m unittest discover -s tests -p 'test_regression.py' -v
python -B -m unittest discover -s tests -p 'test_admin.py' -v
```

`tests/test_live.py`、`tests/test_http.py` 和 `tests/test_policy_live.py` 使用真实上游，可能产生费用。
运行 HTTP 实测前，应在测试进程中设置 `NUTRIENT2API_TOKEN` 和可选的 `NUTRIENT2API_TEST_BASE`。
