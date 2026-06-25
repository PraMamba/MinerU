# LLM Wiki 接入本地 MinerU v4 适配器

本文档用于把自部署 MinerU 接入 LLM Wiki。这里的 `mineru-v4-adapter` 是一个最小兼容层，只覆盖 LLM Wiki 当前调用的 MinerU `/api/v4` 子集，不是 MinerU 官方云 API 的完整复刻。

## 1. 架构

```text
LLM Wiki
  -> http://127.0.0.1:8888/api/v4/...
      mineru-v4-adapter
        -> http://127.0.0.1:8000/tasks
        -> http://127.0.0.1:8000/tasks/{task_id}
        -> http://127.0.0.1:8000/tasks/{task_id}/result
```

需要启动两个服务：

- `mineru-api`：真正执行 PDF 解析。
- `mineru-v4-adapter`：把 LLM Wiki 的 `/api/v4` 请求转换成本地 `mineru-api` 请求。

## 2. 前置检查

进入 MinerU 源码目录：

```bash
cd /root/MinerU
```

如果 `mineru-v4-adapter` 命令不可用，刷新 editable 安装的 console script：

```bash
pip install -e /root/MinerU --no-deps
```

不刷新安装也可以用模块方式启动：

```bash
python -m mineru.cli.v4_adapter --help
```

如果要强制只使用本地模型，确保 `~/mineru.json` 已经配置本地模型路径，然后在启动 `mineru-api` 前设置：

```bash
export MINERU_MODEL_SOURCE=local
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

## 3. 手动启动

### 3.1 启动本地 MinerU API

终端 1：

```bash
cd /root/MinerU

export MINERU_MODEL_SOURCE=local
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

mineru-api --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

### 3.2 启动 v4 适配器

终端 2：

```bash
cd /root/MinerU

python -m mineru.cli.v4_adapter \
  --host 127.0.0.1 \
  --port 8888 \
  --upstream-url http://127.0.0.1:8000 \
  --token local-mineru-token
```

如果 `mineru-v4-adapter` 命令已经可用，也可以写成：

```bash
mineru-v4-adapter \
  --host 127.0.0.1 \
  --port 8888 \
  --upstream-url http://127.0.0.1:8000 \
  --token local-mineru-token
```

健康检查：

```bash
curl http://127.0.0.1:8888/health
```

## 4. 使用启动脚本

仓库中提供了脚本：

```bash
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh
```

赋予执行权限：

```bash
chmod +x /root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh
```

启动：

```bash
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh start
```

查看状态：

```bash
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh status
```

查看日志：

```bash
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh logs
```

重启：

```bash
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh restart
```

停止：

```bash
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh stop
```

常用环境变量覆盖：

```bash
MINERU_REPO=/root/MinerU \
MINERU_API_PORT=8000 \
MINERU_V4_ADAPTER_PORT=8888 \
MINERU_V4_ADAPTER_TOKEN=local-mineru-token \
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh start
```

如果需要让 `/api/v4/extract/task` 解析任意公网 URL，显式开启：

```bash
MINERU_V4_ADAPTER_ALLOW_URL_FETCH=1 \
/root/MinerU/scripts/mineru_llm_wiki_v4_adapter.sh restart
```

默认不建议开启。关闭时，LLM Wiki 设置页固定测试 URL 会使用适配器内置样例文件，不需要联网下载。

## 5. LLM Wiki 配置

修改 LLM Wiki 中的 MinerU API 地址：

```ts
const API_BASE = "http://127.0.0.1:8888/api/v4"
```

在 `app-state.json` 中启用：

```json
"mineruConfig": {
  "enabled": true,
  "token": "local-mineru-token",
  "modelVersion": "pipeline"
}
```

`modelVersion` 可选：

- `pipeline`：传统解析管线，速度更快，适合常规文档。
- `vlm`：视觉语言模型解析，适合复杂排版、公式、表格。

## 6. curl 验证上传解析流程

### 6.1 创建 batch

```bash
curl -s -X POST http://127.0.0.1:8888/api/v4/file-urls/batch \
  -H "Authorization: Bearer local-mineru-token" \
  -H "Content-Type: application/json" \
  -d '{"files":[{"name":"demo1.pdf","data_id":"demo1.pdf"}],"model_version":"pipeline"}'
```

记录返回值：

- `data.batch_id`
- `data.file_urls[0]`

### 6.2 上传 PDF

```bash
UPLOAD_URL="上一步返回的 file_urls[0]"

curl -X PUT "$UPLOAD_URL" \
  --data-binary "@/root/MinerU/demo/pdfs/demo1.pdf"
```

### 6.3 轮询 batch

```bash
BATCH_ID="上一步返回的 batch_id"

curl -s \
  -H "Authorization: Bearer local-mineru-token" \
  "http://127.0.0.1:8888/api/v4/extract-results/batch/${BATCH_ID}"
```

当返回 `state` 为 `done` 后，记录 `full_zip_url`。

### 6.4 下载解析结果

```bash
ZIP_URL="上一步返回的 full_zip_url"

curl -L "$ZIP_URL" -o /tmp/mineru_result.zip
unzip -l /tmp/mineru_result.zip
```

LLM Wiki 会从 zip 中优先读取 `full.md`，如果没有则读取任意 Markdown 文件。

## 7. curl 验证 URL 任务流程

默认关闭任意 URL 拉取时，以下 LLM Wiki 设置页测试 URL 会走本地内置样例：

```bash
curl -s -X POST http://127.0.0.1:8888/api/v4/extract/task \
  -H "Authorization: Bearer local-mineru-token" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://cdn-mineru.openxlab.org.cn/demo/example.pdf","model_version":"pipeline"}'
```

记录返回的 `data.task_id`，然后轮询：

```bash
TASK_ID="上一步返回的 task_id"

curl -s \
  -H "Authorization: Bearer local-mineru-token" \
  "http://127.0.0.1:8888/api/v4/extract/task/${TASK_ID}"
```

当 `state` 为 `done` 后，下载返回的 `full_zip_url`。

## 8. 日志和排错

脚本默认日志目录：

```bash
/tmp/mineru_llm_wiki_v4_adapter/logs
```

查看：

```bash
tail -f /tmp/mineru_llm_wiki_v4_adapter/logs/mineru-api.log
tail -f /tmp/mineru_llm_wiki_v4_adapter/logs/mineru-v4-adapter.log
```

常见问题：

- `401 Missing MinerU bearer token`：LLM Wiki 的 token 为空，或请求没有 `Authorization: Bearer ...`。
- `401 Invalid MinerU bearer token`：LLM Wiki token 与适配器 `--token` 不一致。
- `400 URL fetching is disabled`：任意公网 URL 拉取默认关闭；如确需开启，设置 `MINERU_V4_ADAPTER_ALLOW_URL_FETCH=1`。
- `502 Failed to submit/query MinerU task`：适配器能访问，但底层 `mineru-api` 返回错误，先看 `mineru-api.log`。
- `503`：底层服务不可用、模型初始化失败，或离线模型路径配置不完整。

## 9. 生产部署建议

- 默认只绑定 `127.0.0.1`，通过 LLM Wiki 所在机器本地访问。
- 如需暴露到局域网，优先在反向代理层做访问控制和 TLS。
- `full_zip_url` 和上传 URL 是能力 URL，不带认证头；不要把适配器直接暴露到不可信网络。
- 适配器任务状态保存在进程内存中，重启适配器后无法继续查询旧任务。
- 如果使用本地模型，启动底层 `mineru-api` 时保持 `MINERU_MODEL_SOURCE=local`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。
