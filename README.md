# agent-to-agent

把數個 skill 各自包成 [A2A](https://github.com/a2aproject/a2a-samples) agent，並用一個 curated registry 讓 client 自動發現、委派。

- **Agents**：四個 A2A agent，各自宣告 skill 對外服務：
  - `code_review`：讀根目錄（`CODE_REVIEW_REPO_PATH`）原始碼 → 套 prompt 呼叫 OpenAI 做 code review。
  - `translation`：中英雙向翻譯。
  - `uppercase`：文字轉大寫。
  - `image_analyzer`：呼叫 vision 模型描述圖片。
- **Registry**：維護可信 agent 清單（`REGISTRY_AGENT_URLS`，預設四個內建 agent），提供查詢 / 依 skill 篩選。收錄採 **pull**：查詢時 registry 主動去打每個 agent 的 `/.well-known/agent-card.json`（A2A 標準）抓名片。
- **Client（router）**：收需求 → ARD 上 registry 做語義搜尋取 top-k 候選 → LLM 從候選挑選 → 委派。

## 安裝

```bash
uv sync
cp .env.example .env   # 填入 OPENAI_API_KEY
```

## 執行

建議分成 4 個角色各開一個終端機（獨立 process、不同 port）：

### 1. Server agent

```bash
uv run python -m code_review_agent      # 8001
uv run python -m translation_agent      # 8002
uv run python -m uppercase_agent        # 8003
uv run python -m image_analyzer_agent   # 8004
```

這個腳本一鍵啟動四個 agent：

```bash
bash scripts/run_a2a_ard_agents.sh
# 看名片：curl http://127.0.0.1:8001/.well-known/agent-card.json
```

### 2. Registry
```bash
uv run python registry.py    # 8000
# 查目錄：curl "http://127.0.0.1:8000/agents"
```

### 3. Client agent
```bash
uv run python registry_client.py                  # 互動輸入需求，經 registry 委派
uv run python -m translation_agent.test_client    # 或直連指定 agent
```


## 認證（OAuth2 / OIDC，選用）

預設無認證；`.env` 有 `A2A_OIDC_*` 就自動啟用。啟用後 agent 與 registry 都對每個請求驗 JWT（缺/壞 token → 401、IdP 連不上 → 503）；agent 另查 scope（不足 → 403），registry 只驗身分。client 會自動換 token 帶上。驗過的請求會在 server log 印一行 `OIDC ✓ 通過 …`。

本機用 [Keycloak](https://www.keycloak.org/) 當 IdP、[Colima](https://github.com/abiosoft/colima) 當容器：

```bash
# 前置
brew install colima docker
colima start
bash scripts/setup_keycloak.sh   # 建 realm/client/scope 並寫入 .env

# 確保 IdP 在跑，再照上面〈執行〉啟動
docker start a2a-keycloak         

# 驗證有生效（兩者都應回 401）
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8001/    # agent
curl -s -o /dev/null -w "%{http_code}\n"      http://127.0.0.1:8000/agents # registry

# 收工
docker stop a2a-keycloak          # 保留資料，下次 docker start 續用
colima stop                       # 不用容器時關 VM
```

## 檔案結構

共有四個 agent（`code_review` / `translation` / `uppercase` / `image_analyzer`），結構一致，每個資料夾下：

```
code_review_agent/
├── __main__.py         # AgentCard（名片 / skill / port）+ server 進入點
├── agent_executor.py   # agent 實際邏輯（收訊息 → 處理 → 回 artifact）
├── prompt.md           # 僅 code_review 有：審查標準（system prompt）
└── test_client.py      # 直連此 agent 的測試 client
```

共用模組：

```
shared/
├── auth.py     # OAuth2/OIDC 驗證（server middleware + client 取 token）
├── server.py   # A2A app 組裝（card 安全宣告、路由、middleware）
└── client.py   # 最小 A2A client：連線、送 parts、串流印回應
```

最外層：

```
registry.py         # curated registry 服務：聚合各 agent 名片、提供語義搜尋
registry_client.py  # router client：經 registry 挑 agent 並委派需求
```

改東西：agent 邏輯改 `agent_executor.py`；名片/skill/port 改 `__main__.py`；審查標準改 `prompt.md`；收錄哪些 agent 改 `REGISTRY_AGENT_URLS`。
