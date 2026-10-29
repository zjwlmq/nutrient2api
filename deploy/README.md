# Linux 服务器部署

使用 Docker Compose 运行 nutrient2api，由现有宝塔、1Panel 或 Nginx 提供域名和 HTTPS。
服务默认只发布到宿主机 `127.0.0.1:8787`。公网仅开放 HTTPS 和证书签发所需端口。
该部署适合受控的小规模使用；服务仍基于 Python 标准库 HTTP 服务器。

## 准备项目和密钥

将整个项目上传到服务器，例如 `/opt/nutrient2api`，在该目录执行：

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
nano .env
```

填写两个必需值：

```dotenv
NUTRIENT_API_KEY=填写具备_AI_Gateway_权限的_Nutrient_密钥
NUTRIENT2API_TOKEN=填写上一步生成的随机字符串
```

Nutrient 密钥用于连接上游；随机访问令牌用于你的客户端连接本服务，两者不同。
不要把 `.env` 放到网站静态目录。Docker 构建只复制 Python 源码，密钥不会进入镜像。
程序本身不自动读取 `.env`；本方案由 Compose 注入环境变量。

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
修改 `.env` 后执行 `docker compose up -d --force-recreate`；单独 restart 不会重新注入环境变量。

## 面板和域名

1. 将域名的 DNS 记录指向服务器。
2. 在现有面板创建反向代理网站，绑定域名，申请并启用 HTTPS。
3. 若面板代理运行在宿主机上，目标填写 `http://127.0.0.1:8787`，保留原请求路径和 Authorization 请求头。
4. 关闭响应缓冲与缓存，设置上游读取空闲超时为 360 秒。保留请求缓冲，以便传给 Python 的请求带有 Content-Length。
5. 设置请求体上限 10 MiB、读取超时及访问频率限制。独立 Nginx 的完整模板见 [nginx.conf.example](nginx.conf.example)。

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

客户端选择 Chat Completions 兼容接口：

- Base URL：`https://你的域名/v1`
- API Key：`.env` 中的 `NUTRIENT2API_TOKEN`
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

更新前备份项目源代码和 `.env`，并保留旧镜像。更新代码后执行 `docker compose up -d --build`。
需要回退时，恢复旧代码并重建。服务没有本地业务数据库。
默认限制为 32 个活动连接、10 MiB 请求体、30 秒客户端套接字空闲超时，以及 256 MiB 容器内存。
容器日志轮转为 3 个 10 MiB 文件。大图片、长上下文或更高并发需要按实测调整请求体和内存限制。

遇到工具与推理参数冲突时，服务会调用 Responses 获取完整结果，再封装为 SSE；这条回退路径会推迟首个内容事件。
上游断流会作为 SSE error 返回，客户端应检查错误事件，不能只检查 HTTP 200。
非流式上游 HTTP 429/5xx 会有限重试并退避；生成请求遇到网络超时不会自动重发，以减少重复执行。
Compose 的环境变量和容器限制配置见 [Docker 官方说明](https://docs.docker.com/reference/compose-file/services/)。

## 通过 GitHub 更新

项目仓库：[zjwlmq/nutrient2api](https://github.com/zjwlmq/nutrient2api)。
私有仓库需要先完成 GitHub 认证，服务器可使用只读 Deploy key。
配置好 HTTPS 认证后，首次部署可执行：

```bash
git clone https://github.com/zjwlmq/nutrient2api.git
cd nutrient2api
```

如果服务器使用 SSH Deploy key，改用 `git@github.com:zjwlmq/nutrient2api.git` 克隆。
然后按前文配置 `.env` 并启动。
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
`.env` 已加入 Git 忽略规则，正常拉取更新不会覆盖密钥。
服务器专用的网络配置可保存在 `compose.override.yaml`；建议将该文件加入服务器上的 `.git/info/exclude`，避免误提交。
面板中的站点和证书配置独立于此仓库。

## 本地回归

这些测试使用本地模拟上游，不需要 API 密钥，也不产生模型调用费用：

```bash
python -B tests/test_prompt.py
python -B tests/test_stream_scrub.py
python -B -m unittest discover -s tests -p 'test_regression.py' -v
```

`tests/test_live.py`、`tests/test_http.py` 和 `tests/test_policy_live.py` 使用真实上游，可能产生费用。
运行 HTTP 实测前，应在测试进程中设置 `NUTRIENT2API_TOKEN` 和可选的 `NUTRIENT2API_TEST_BASE`。
