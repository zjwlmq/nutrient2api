"use strict";

let docsTopic = "import", docsLanguage = "bash";
const docsTopics = {import:"上传账号 Key", chat:"聊天调用", models:"模型与嵌入", help:"维护与排错"};
const docsLanguages = {bash:"Bash / cURL", powershell:"Windows PowerShell", python:"Python"};
const docsRequests = {
  import: {title:"添加一个账号", method:"POST", path:"/api/keys", token:"导入令牌",
    body:{name:"主账户", api_key:"替换为Nutrient账号Key", enabled:true}},
  chat: {title:"普通聊天", method:"POST", path:"/v1/chat/completions", token:"访问令牌",
    body:{model:"gpt-6-luna", messages:[{role:"user", content:"请回复：你好"}], stream:false, max_completion_tokens:2000, store:false}},
  stream: {title:"流式聊天", method:"POST", path:"/v1/chat/completions", token:"访问令牌",
    body:{model:"gpt-6-luna", messages:[{role:"user", content:"请回复：你好"}], stream:true, max_completion_tokens:2000, store:false}},
  models: {title:"获取模型列表", method:"GET", path:"/v1/models", token:"访问令牌"},
  embeddings: {title:"生成文本嵌入", method:"POST", path:"/v1/embeddings", token:"访问令牌",
    body:{model:"text-embedding-3-small", input:["你好", "Hello"]}}
};
const docsCommands = {
  setup: "cd ~/nutrient2api &&\ndocker compose exec -T nutrient2api python -m nutrient2api setup-code",
  client: "cd ~/nutrient2api &&\ndocker compose exec -T nutrient2api python -m nutrient2api client-token",
  import: "cd ~/nutrient2api &&\ndocker compose exec -T nutrient2api python -m nutrient2api key-import-token",
  rotate: "cd ~/nutrient2api &&\ndocker compose exec -T nutrient2api python -m nutrient2api rotate-key-import-token",
  reset: "cd ~/nutrient2api &&\ndocker compose exec -T nutrient2api python -m nutrient2api reset-admin",
  update: "cd ~/nutrient2api &&\ngit pull --ff-only &&\ndocker compose up -d --build --wait &&\ndocker compose ps"
};

function docsExample(kind, language = docsLanguage) {
  const request = docsRequests[kind];
  const url = location.origin + request.path;
  const token = "替换为" + request.token;
  const json = request.body ? JSON.stringify(request.body, null, 2) : null;
  if (language === "python") {
    return "import json\nfrom urllib.request import Request, urlopen\n\n" +
      `token = ${JSON.stringify(token)}\n` +
      (json ? `payload = json.loads(${JSON.stringify(JSON.stringify(request.body))})\n` : "") +
      `request = Request(\n    ${JSON.stringify(url)},\n` +
      (json ? '    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),\n' : "") +
      '    headers={"Authorization": "Bearer " + token' + (json ? ', "Content-Type": "application/json"' : "") + '},\n' +
      `    method="${request.method}",\n)\n` +
      'with urlopen(request, timeout=120) as response:\n' +
      (kind === "stream" ? '    for line in response:\n        print(line.decode("utf-8").rstrip(), flush=True)' :
        '    print(json.dumps(json.load(response), ensure_ascii=False, indent=2))');
  }
  if (language === "powershell") {
    const prefix = `$token = '${token}'\n` + (json ? `$body = @'\n${json}\n'@\n` : "");
    if (kind === "stream") {
      // A UTF-8 file avoids native-command JSON quoting differences between PowerShell versions.
      return prefix + '$bodyFile = [System.IO.Path]::GetTempFileName()\ntry {\n' +
        '    [System.IO.File]::WriteAllText($bodyFile, $body, [System.Text.UTF8Encoding]::new($false))\n' +
        `    curl.exe --fail-with-body -N '${url}' \`\n` +
        '      -H "Authorization: Bearer $token" `\n' +
        '      -H "Content-Type: application/json" `\n' +
        '      --data-binary "@$bodyFile"\n' +
        '} finally {\n    Remove-Item -LiteralPath $bodyFile\n}';
    }
    return prefix + `Invoke-RestMethod -Method ${request.method} -Uri '${url}' \`\n` +
      '  -Headers @{ Authorization = "Bearer $token" }' +
      (json ? ' `\n  -ContentType "application/json; charset=utf-8" `\n  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))' : "") +
      ' | ConvertTo-Json -Depth 20';
  }
  return `TOKEN='${token}'\ncurl --fail-with-body${kind === "stream" ? " -N" : ""} '${url}' \\\n` +
    '  -H "Authorization: Bearer $TOKEN"' +
    (json ? " \\\n  -H 'Content-Type: application/json' \\\n  --data-binary @- <<'JSON'\n" + json + "\nJSON" : "");
}

function docsCode(kind) {
  const request = docsRequests[kind];
  return `<section class="docs-code-card"><div class="docs-code-heading"><div><h3>${request.title}</h3><span class="docs-method">${request.method}</span> <code>${request.path}</code></div>
    <button class="secondary" data-copy-example="${kind}" aria-label="复制${request.title}示例">${icon("copy")}复制示例</button></div>
    <pre tabindex="0" aria-label="${request.title}代码"><code>${esc(docsExample(kind))}</code></pre></section>`;
}

function docsCommand(kind, title, description) {
  return `<details class="docs-command"><summary>${title}</summary><p>${description}</p><div class="docs-command-code">
    <pre tabindex="0" aria-label="${title}命令"><code>${esc(docsCommands[kind])}</code></pre>
    <button class="secondary" data-copy-command="${kind}" aria-label="复制${title}命令">${icon("copy")}复制命令</button></div></details>`;
}

function docsContent() {
  if (docsTopic === "import") return `<div class="docs-section-intro"><h2>把多个账号放进同一个池子</h2><p>每个账号提供一个有 AI Gateway 权限的 Nutrient Key；每次上传一个，保存后立即生效。可添加最多 50 个不同的 Key。</p></div>
    ${docsCode("import")}
    <dl class="docs-fields"><div><dt>api_key <span>必填</span></dt><dd>Nutrient 提供的账号 Key。无需提交账号邮箱或密码。</dd></div><div><dt>name <span>可选</span></dt><dd>便于辨认的名称，例如“主账户”；省略时自动命名。</dd></div><div><dt>enabled <span>可选</span></dt><dd>默认为 true，立即参与调度；false 表示先保存为停用状态。</dd></div></dl>
    <div class="docs-note"><strong>导入成功后会发生什么？</strong><p>新 Key 返回 201 和 <code>created: true</code>；重复上传返回 200 和 <code>created: false</code>，保留原名称、启停状态和统计。每次请求只能提交一个 JSON 对象，请求体最多 16 KiB。</p><p>导入成功只表示已保存。实际能否调用，取决于 Key 的权限和额度；可在<a href="#accounts">账户管理</a>中检测模型列表连通性。</p></div>`;
  if (docsTopic === "chat") return `<div class="docs-section-intro"><h2>使用一个接口调用整个账号池</h2><p>客户端选择 OpenAI 兼容的 Chat Completions 模式，Base URL 填上方地址，API Key 填“访问令牌”。示例会实际调用上游，消耗账号额度。</p></div>
    ${docsCode("chat")}<p class="docs-caption">普通回复在 JSON 的 <code>choices[0].message.content</code> 中读取。</p>
    ${docsCode("stream")}<div class="docs-note"><strong>如何读取流式回复？</strong><p>逐条读取 <code>data:</code> 事件，内容在 <code>choices[0].delta.content</code>，遇到 <code>data: [DONE]</code> 结束。流开始后也可能收到 <code>error</code> 事件，请同时检查事件内容。</p><p>本服务未开放 <code>/v1/responses</code>。只支持 Responses 的客户端无法直接接入。</p></div>`;
  if (docsTopic === "models") return `<div class="docs-section-intro"><h2>先查看模型，再选择调用方式</h2><p>模型列表来自本服务的本地映射，不调用上游，也不消耗模型额度。列表中出现的模型是否能调用，以账号权限和额度为准。</p></div>
    ${docsCode("models")}<p class="docs-caption">聊天示例使用 <code>gpt-6-luna</code>，也可使用网关别名 <code>nutrient-s</code>；完整列表以接口返回为准。</p>
    ${docsCode("embeddings")}<p class="docs-caption">嵌入结果在 <code>data[i].embedding</code> 中。此请求会调用上游并消耗额度。</p>`;
  return `<div class="docs-section-intro"><h2>常用命令与问题速查</h2><p>下列命令在服务器终端运行，项目目录默认为 <code>~/nutrient2api</code>。点击展开后即可复制。</p></div>
    <div class="docs-commands">
      ${docsCommand("setup", "初始化码在哪里？", "初始化码只用于首次设置管理员密码，使用后失效；管理员密码至少 12 个字符，负责登录后台。它们与账号 Key、API 令牌用途不同。")}
      ${docsCommand("client", "在服务器获取访问令牌", "用于客户端 API Key 字段，或聊天、模型与嵌入接口的 Bearer 请求头。也可在本页顶部直接复制。")}
      ${docsCommand("import", "在服务器获取导入令牌", "仅用于 POST /api/keys，不需要先初始化网页管理员；也可登录后在本页顶部复制。")}
      ${docsCommand("rotate", "更换导入令牌", "执行后旧导入令牌立即失效，请更新所有上传 Key 的脚本。已有账号保持可用，无需重启服务。")}
      ${docsCommand("reset", "忘记管理员密码", "此命令清除旧管理员密码并使旧登录会话失效，输出新的初始化码。刷新后台后重新设置密码，已保存账号和 API 令牌保留。")}
      ${docsCommand("update", "更新服务器程序", "先拉取 GitHub 更新，再重建并等待容器健康。账号和令牌保存在 Docker 数据卷；不要执行 docker compose down -v，它会删除数据卷。")}
    </div>
    <h3 class="docs-subheading">常见返回码</h3><div class="docs-errors">
      ${[["400", "请求格式不正确", "检查 JSON、字段名称及模型是否适合当前接口；导入接口只接受 api_key、name、enabled。"], ["401", "令牌不正确", "上传 Key 使用导入令牌；聊天、模型、嵌入使用访问令牌。请求头必须是 Authorization: Bearer 令牌。"], ["403", "权限不足", "检查账号是否有上游 AI Gateway 权限；后台操作则可能需要重新登录。"], ["413", "请求体太大", "Key 导入接口上限 16 KiB，聊天和嵌入请求默认上限 10 MiB。"], ["429", "请求受限", "稍后再试，并在账户管理中查看账号冷却状态。"], ["503", "暂时没有可用账号", "添加有效 Key、启用账号，或等待处于冷却中的账号恢复。"], ["502 / 504", "上游异常或超时", "查看调用记录，并检查上游可用性与服务器网络。"]].map(([code,title,description]) => `<div><code>${code}</code><section><h4>${title}</h4><p>${description}</p></section></div>`).join("")}
    </div><div class="docs-note"><strong>健康检查通过，为什么还不能聊天？</strong><p><code>GET /health</code> 不需要令牌，只检查程序是否运行，不检查账号 Key、权限或余额。导入有效 Key 后，还需要完成一次真实调用。</p></div>`;
}

function renderDocsBody() {
  $("#docs-body").innerHTML = docsContent();
  $("#docs-language-bar").hidden = docsTopic === "help";
  document.querySelectorAll("[data-docs-topic]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.docsTopic === docsTopic)));
  document.querySelectorAll("[data-docs-language]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.docsLanguage === docsLanguage)));
  $("#docs-body").querySelectorAll("[data-copy-example]").forEach(button => button.addEventListener("click", () => busy(button, () => copy(docsExample(button.dataset.copyExample), "调用示例"))));
  $("#docs-body").querySelectorAll("[data-copy-command]").forEach(button => button.addEventListener("click", () => busy(button, () => copy(docsCommands[button.dataset.copyCommand], "服务器命令"))));
}

function renderDocs() {
  $("#content").innerHTML = `<header class="page-heading"><div><h1>API 使用说明</h1><p class="subtitle">地址、令牌、调用示例，随时查看与复制。</p></div><span class="docs-badge">OpenAI 兼容接口</span></header>
    <div class="docs-page"><section class="panel docs-connect"><div class="docs-connect-heading"><h2>连接信息</h2><p>地址自动跟随当前访问域名。</p></div>
      <div class="connection-row"><label for="docs-base-url">Base URL</label><div class="copy-field"><input id="docs-base-url" readonly value="${esc(location.origin + "/v1")}"><button data-copy-address="/v1" aria-label="复制客户端 Base URL">${icon("copy")}</button></div></div>
      <div class="connection-row"><label for="docs-import-url">Key 导入地址</label><div class="copy-field"><input id="docs-import-url" readonly value="${esc(location.origin + "/api/keys")}"><button data-copy-address="/api/keys" aria-label="复制 Key 导入地址">${icon("copy")}</button></div></div>
      <div class="docs-token-actions"><button class="primary" id="docs-copy-client">${icon("copy")}复制访问令牌</button><button class="secondary" id="docs-copy-import">${icon("copy")}复制导入令牌</button><small>令牌仅在点击时读取。</small></div></section>
    <section class="docs-credentials" aria-label="三种 Key 和令牌的用途">
      <div><span>01 / 来自 Nutrient</span><h3>账号 Key</h3><p>每个账号一个，上传后加入账号池，用于连接上游。</p></div>
      <div><span>02 / 客户端使用</span><h3>访问令牌</h3><p>填入客户端 API Key 字段，用于聊天、模型列表和嵌入。</p></div>
      <div><span>03 / 上传 Key 使用</span><h3>导入令牌</h3><p>调用 Key 导入接口时使用，可反复添加多个账号。</p></div>
    </section>
    <nav class="docs-topics" aria-label="文档主题">${Object.entries(docsTopics).map(([key,label]) => `<button data-docs-topic="${key}" aria-pressed="${key === docsTopic}">${label}</button>`).join("")}</nav>
    <div class="docs-language-bar" id="docs-language-bar"><div class="docs-languages" role="group" aria-label="示例语言">${Object.entries(docsLanguages).map(([key,label]) => `<button data-docs-language="${key}" aria-pressed="${key === docsLanguage}">${label}</button>`).join("")}</div><p>复制后替换示例中的“替换为…”占位内容。</p></div>
    <div id="docs-body"></div></div>`;
  document.querySelectorAll("[data-copy-address]").forEach(button => button.addEventListener("click", () => busy(button, () => copy(location.origin + button.dataset.copyAddress, "接口地址"))));
  $("#docs-copy-client").addEventListener("click", event => busy(event.currentTarget, async () => copy((await api("client-token", {})).token, "访问令牌")));
  $("#docs-copy-import").addEventListener("click", event => busy(event.currentTarget, async () => copy((await api("key-import-token", {})).token, "导入令牌")));
  document.querySelectorAll("[data-docs-topic]").forEach(button => button.addEventListener("click", () => { docsTopic = button.dataset.docsTopic; renderDocsBody(); }));
  document.querySelectorAll("[data-docs-language]").forEach(button => button.addEventListener("click", () => { docsLanguage = button.dataset.docsLanguage; renderDocsBody(); }));
  renderDocsBody();
}
