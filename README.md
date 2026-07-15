# agent-to-agent

把一個 code-review skill 包成 [A2A](https://github.com/a2aproject/a2a-samples) agent，並用一個 curated registry 讓 client 自動發現、委派。

- **Agent**：讀取自己根目錄（`CODE_REVIEW_REPO_PATH`）的原始碼 → 套 code-review system prompt 呼叫 OpenAI → 回傳 review。
- **Registry**：維護可信 agent 清單，提供查詢 / 依 skill 篩選。
- **Client（router）**：收需求 → 上 registry 用 LLM 挑合適 agent → 委派。

registry 收錄 agent 有兩種方式：**pull**（registry 讀 `REGISTRY_AGENT_URLS` 主動抓名片）或 **push**（agent 帶 `REGISTRY_URL` 啟動時自我註冊到 `/register`）。client 只需知道 registry 位址。

## 安裝

```bash
uv sync
cp .env.example .env   # 填入 OPENAI_API_KEY
```

## 執行

三個角色各開一個終端機（獨立 process、不同 port）。

```bash
# 1. Agent（:9999）
uv run python -m code_review_agent
#    看名片：curl http://127.0.0.1:9999/.well-known/agent-card.json

# 2. Registry（:8000）—— pull（白名單）或 push（留空清單由 agent 自報）
REGISTRY_AGENT_URLS="http://127.0.0.1:9999" uv run python registry.py
#    查目錄：curl "http://127.0.0.1:8000/agents?skill=code_review"

# 3. Client（router）
uv run python registry_client.py                  # 互動輸入需求
uv run python -m code_review_agent.test_client    # 或直連指定 agent
```

## 檔案結構

```
code_review_agent/
├── __main__.py         # AgentCard/AgentSkill 定義 + 起 A2A server（含 self-registration）
├── agent_executor.py   # A2A 協定黏著層 + 讀 repo、呼叫 LLM 的邏輯
├── prompt.md           # code-review skill 本體（LLM 的 system prompt）
└── test_client.py      # 直連 agent 的測試 client
registry.py             # curated registry 服務
registry_client.py      # 經 registry 找 agent 並委派的 router client
```

改東西：審查標準改 `prompt.md`；LLM 後端改 `agent_executor.py`；名片/skill/port 改 `__main__.py` 的 `build_agent_card()`；收錄哪些 agent 改 `REGISTRY_AGENT_URLS`。

## 限制與安全

- agent 會把 `CODE_REVIEW_REPO_PATH` 下 **git 追蹤的文字檔**送至外部 LLM，經三層過濾：`.gitignore` → 機密黑名單（`.env`、`.pem`、`.key`、`id_rsa`…）→ 二進位檔與超過 100KB 的檔略過。仍請勿對含機密的 repo 直接使用。
- self-registration 未設 `REGISTRY_REGISTER_TOKEN` 時任何人都能註冊，僅建議用於本機/受控內網。
- router 用 LLM 選 agent，輸出具**非決定性**，可能選不到（會回報並停止）。
