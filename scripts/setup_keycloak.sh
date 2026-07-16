# 一鍵起本機 Keycloak，建好 realm / client / scope / audience，並把 OAuth 變數寫進 .env。
# 可重複執行：
#   啟用：bash scripts/setup_keycloak.sh
#   收工：docker rm -f a2a-keycloak
#
# 僅供本機開發，不要用於任何對外／正式環境。
#
set -euo pipefail

REALM=a2a
CLIENT_ID=code-review-client
SCOPE=code_review.invoke
AUDIENCE=code-review-agent
CONTAINER=a2a-keycloak
BASE=http://localhost:8080
ENV_FILE="${ENV_FILE:-.env}"

RT=$(command -v docker || command -v podman) \
  || { echo "✗ 需要 docker 或 podman：brew install colima docker && colima start" >&2; exit 1; }
"$RT" info >/dev/null 2>&1 || { echo "✗ container daemon 沒在跑（colima start?）" >&2; exit 1; }

# 1. 起 Keycloak（已存在就重用）
if ! "$RT" ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  "$RT" run -d --name "$CONTAINER" -p 8080:8080 \
    -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
    quay.io/keycloak/keycloak:26.0 start-dev >/dev/null
fi
"$RT" start "$CONTAINER" >/dev/null 2>&1 || true

# 2. 等就緒
echo -n "▶ 等待 Keycloak"
until curl -sf "$BASE/realms/master/.well-known/openid-configuration" >/dev/null; do echo -n .; sleep 2; done
echo " ✓"

# 3. admin token + API helper（回傳 HTTP 狀態碼，body 寫到 $BODY）
TOKEN=$(curl -s "$BASE/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d username=admin -d password=admin -d grant_type=password | jq -r .access_token)
BODY=$(mktemp); trap 'rm -f "$BODY"' EXIT
api() {  # api METHOD PATH [JSON]
  local args=(-s -o "$BODY" -w '%{http_code}' -X "$1" "$BASE$2" -H "Authorization: Bearer $TOKEN")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  curl "${args[@]}"
}
ok() { [[ "$1" =~ ^(201|204|409)$ ]] || { echo "✗ HTTP $1: $(cat "$BODY")" >&2; exit 1; }; }

# 4. realm
ok "$(api POST /admin/realms "$(jq -n --arg r "$REALM" '{realm:$r, enabled:true}')")"

# 5. client scope（含 audience mapper）
ok "$(api POST "/admin/realms/$REALM/client-scopes" "$(jq -n --arg s "$SCOPE" --arg a "$AUDIENCE" '{
  name:$s, protocol:"openid-connect",
  attributes:{"include.in.token.scope":"true"},
  protocolMappers:[{name:"a2a-aud", protocol:"openid-connect", protocolMapper:"oidc-audience-mapper",
    config:{"included.custom.audience":$a, "access.token.claim":"true", "id.token.claim":"false"}}]}')")"

# 6. client（confidential + service account = client credentials）
ok "$(api POST "/admin/realms/$REALM/clients" "$(jq -n --arg c "$CLIENT_ID" '{
  clientId:$c, protocol:"openid-connect", publicClient:false, serviceAccountsEnabled:true,
  standardFlowEnabled:false, directAccessGrantsEnabled:false}')")"

# 7. 綁 scope → client 的 default scope
CUID=$(curl -s "$BASE/admin/realms/$REALM/clients?clientId=$CLIENT_ID" -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')
SID=$(curl -s "$BASE/admin/realms/$REALM/client-scopes" -H "Authorization: Bearer $TOKEN" | jq -r --arg n "$SCOPE" '.[]|select(.name==$n).id')
ok "$(api PUT "/admin/realms/$REALM/clients/$CUID/default-client-scopes/$SID")"

# 8. client secret
SECRET=$(curl -s "$BASE/admin/realms/$REALM/clients/$CUID/client-secret" -H "Authorization: Bearer $TOKEN" | jq -r .value)

# 9. 寫 .env（已有 A2A_OIDC_ISSUER 就只印出、不覆蓋）
BLOCK="
# ── 由 scripts/setup_keycloak.sh 產生 ──
A2A_OIDC_ISSUER=$BASE/realms/$REALM
A2A_OIDC_AUDIENCE=$AUDIENCE
A2A_REQUIRED_SCOPE=$SCOPE
A2A_OAUTH_TOKEN_URL=$BASE/realms/$REALM/protocol/openid-connect/token
A2A_OAUTH_CLIENT_ID=$CLIENT_ID
A2A_OAUTH_CLIENT_SECRET=$SECRET
A2A_OAUTH_SCOPE=$SCOPE"

echo "════════════════════════════════════════"; echo "$BLOCK"; echo "════════════════════════════════════════"
# upsert：先移除舊的 A2A_* 與產生標記，再寫入最新（重跑後 secret 一定同步）
touch "$ENV_FILE"
grep -vE '^(# ── 由 scripts/setup_keycloak|A2A_OIDC_|A2A_OAUTH_|A2A_REQUIRED_SCOPE)' "$ENV_FILE" > "$ENV_FILE.tmp" || true
printf '%s\n' "$BLOCK" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
echo "✓ 已更新 ${ENV_FILE} （舊的 A2A_* 會被最新值取代）"
