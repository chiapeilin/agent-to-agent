# agent-to-agent

把一個 code-review skill 包成 [A2A](https://github.com/a2aproject/a2a-samples) agent，並用一個 curated registry 讓 client 自動發現、委派。

- **Agent**：讀根目錄（`CODE_REVIEW_REPO_PATH`）原始碼 → 套 code-review prompt 呼叫 OpenAI → 回傳 review。
- **Registry**：維護可信 agent 清單，提供查詢 / 依 skill 篩選。收錄方式為 **pull**（讀 `REGISTRY_AGENT_URLS` 主動抓名片）或 **push**（agent 帶 `REGISTRY_URL` 啟動時自報到 `/register`）。
- **Client（router）**：收需求 → 上 registry 用 LLM 挑 agent → 委派。

## 安裝

```bash
uv sync
cp .env.example .env   # 填入 OPENAI_API_KEY
```

## 執行

建議分成 4 個角色各開一個終端機（獨立 process、不同 port）：

```bash
# 1. 內建 code-review agent（:8001）
uv run python -m code_review_agent
#    看名片：curl http://127.0.0.1:8001/.well-known/agent-card.json
uv run python -m translation_agent      # 8002
uv run python -m uppercase_agent        # 8003
uv run python -m image_analyzer_agent   # 8004

# 2. Registry（:8000）
uv run python registry.py
#    查目錄：curl "http://127.0.0.1:8000/agents"
#    查 semantic discover：curl -X POST http://127.0.0.1:8000/search -H 'content-type: application/json' -d '{"query":{"text":"review my Rust code"}}'

# 3. Client
uv run python registry_client.py                  # 互動輸入需求，經 registry 委派
uv run python -m code_review_agent.test_client    # 或直連指定 agent
```

### 一鍵啟動四個 agent

```bash
bash scripts/run_a2a_ard_agents.sh
```

這個腳本會一次啟動以下四個服務：
- Code Review Agent: http://127.0.0.1:8001
- Translation Agent: http://127.0.0.1:8002
- Uppercase Agent: http://127.0.0.1:8003
- Image Analyzer Agent: http://127.0.0.1:8004

## 認證（OAuth2 / OIDC，選用）

預設無認證；`.env` 有 `A2A_OIDC_*` 就自動啟用。啟用後 agent 與 registry 都對每個請求驗 JWT（缺/壞 token → 401、IdP 連不上 → 503）；agent 另查 scope（不足 → 403），registry 只驗身分、`POST /register` 走 `x-registry-token`。client 會自動換 token 帶上。驗過的請求會在 server log 印一行 `OIDC ✓ 通過 …`。

本機用 [Keycloak](https://www.keycloak.org/) 當 IdP、[Colima](https://github.com/abiosoft/colima) 當容器：

```bash
# 前置
brew install colima docker
colima start
bash scripts/setup_keycloak.sh   # 建 realm/client/scope 並寫入 .env

# 之後每次：確保 IdP 在跑，再照上面〈執行〉啟動
docker start a2a-keycloak         # 若已停

# 驗證有生效（兩者都應回 401）
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8001/    # agent
curl -s -o /dev/null -w "%{http_code}\n"      http://127.0.0.1:8000/agents # registry

# 收工
docker stop a2a-keycloak          # 保留資料，下次 docker start 續用
colima stop                       # 完全不用容器時再關 VM
```

## 檔案結構

```
code_review_agent/      # 主要 code-review agent
├── __main__.py
├── agent_executor.py
├── prompt.md
└── test_client.py

shared/                 # 共用 auth / helper 模組
├── __init__.py
└── auth.py

translation_agent/      # 內建 translation sample agent
├── __main__.py
└── server.py

uppercase_agent/        # 內建 uppercase sample agent
├── __main__.py
└── server.py

image_analyzer_agent/   # 內建 image analysis sample agent
├── __main__.py
└── server.py

registry.py             # curated registry 服務
registry_client.py      # 經 registry 找 agent 並委派的 router client
scripts/setup_keycloak.sh  # 一鍵起本機 Keycloak 並產生 OAuth 設定
```

改東西：審查標準改 `prompt.md`；LLM 後端改 `agent_executor.py`；名片/skill/port 改 `__main__.py`；收錄哪些 agent 改 `REGISTRY_AGENT_URLS`。
