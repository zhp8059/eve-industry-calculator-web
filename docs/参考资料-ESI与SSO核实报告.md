# EVE Online ESI / SSO 事实核实报告

> 核实日期：**2026-10-12**（ESI 官方返回的“当前日期(UTC-11)”为 `2026-10-12`，见 `GET https://esi.evetech.net/status/?compatibility_date=2026-10-13` 的 400 报文）。
> 所有结论均附我实际访问过的 URL。凡我无法核实的点，均以 **未核实** 明确标注。
> 本报告不包含代码，只有 URL 与 HTTP 请求示例。

## 0. 核实方法与证据等级

| 等级 | 含义 | 本报告中的例子 |
|---|---|---|
| A 级 | CCP/官方实时接口直接实测 | `login.eveonline.com/.well-known/oauth-authorization-server`、`esi.evetech.net/...` 实测 |
| B 级 | CCP 官方文档（esi-docs 仓库 / developers.eveonline.com 文档与 dev blog） | `esi-docs` 的 sso、rate-limiting、best-practices 等 markdown |
| C 级 | 第三方“由官方 OpenAPI 生成”的 SDK 镜像 | `seatplus/esi-schema`（其 `GeneratedSpec::COMPATIBILITY_DATE = 2026-08-18`，`ROUTE_COUNT = 233`） |

**为什么端点元数据要用 C 级来源**：官方规范 `https://esi.evetech.net/meta/openapi.json`（及 `openapi-3.0.json`）我确实抓到了（HTTP 200，OpenAPI 3.1 / 3.0），但文件极大，我的抓取工具单次会截断到约 100 KB，而该规范的 JSON 键序是 `components` 在前、`paths` 在后，因此**始终拿不到 `paths` 段**；同时本会话的 shell 出网被沙箱阻断（curl/Invoke-WebRequest 均为 `SEC_E_NO_CREDENTIALS`/连接失败），无法下载后本地检索。因此 scope / roles / 分页 / 缓存 / 限流组的逐路由元数据取自 `seatplus/esi-schema`（从官方 OpenAPI 生成，并记录了生成所用的兼容日期），并用官方文档与实时接口交叉验证。**scope 名称、角色名、限流组等请以此为准但知悉其来源为镜像。**

来源（均实际访问）：
- https://esi.evetech.net/meta/openapi.json
- https://esi.evetech.net/meta/openapi-3.0.json
- https://developers.eveonline.com/blog/changing-specs-from-swagger-to-openapi
- https://github.com/seatplus/esi-schema
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/34873696d6854ef5c67539324fc569d3d6fd148f （`src/GeneratedSpec.php`：兼容日期 2026-08-18、233 条路由、304 个 DTO）

---

# A. EVE SSO (OAuth2) 细节

## A1. 授权端点与令牌端点（v1 还是 v2）

官方元数据端点实测返回（A 级证据）：

```http
GET https://login.eveonline.com/.well-known/oauth-authorization-server
```

返回（节选，原样）：

- `authorization_endpoint`: `https://login.eveonline.com/v2/oauth/authorize`
- `token_endpoint`: `https://login.eveonline.com/v2/oauth/token`
- `userinfo_endpoint`: `https://login.eveonline.com/v2/oauth/verify`
- `revocation_endpoint`: `https://login.eveonline.com/v2/oauth/revoke`
- `jwks_uri`: `https://login.eveonline.com/oauth/jwks`
- `issuer`: `https://login.eveonline.com`
- `response_types_supported`: `["code","token"]`
- `code_challenge_methods_supported`: `["S256"]`
- `token_endpoint_auth_methods_supported`: `["client_secret_basic","client_secret_post","client_secret_jwt"]`
- `revocation_endpoint_auth_methods_supported`: `["client_secret_basic","client_secret_post","client_secret_jwt"]`
- `id_token_signing_alg_values_supported`: `["HS256"]`（历史遗留字段，实际 access token 是 RS256 签名的 JWT，见 A5）

**可直接照抄的授权 URL（含全部参数）**

```http
GET https://login.eveonline.com/v2/oauth/authorize
    ?response_type=code
    &redirect_uri=<URL 编码后的回调地址，必须与注册值完全一致>
    &client_id=<你的 client_id>
    &scope=esi-characters.read_blueprints.v1%20esi-industry.read_character_jobs.v1
    &state=<随机字符串，防 CSRF>
    [&code_challenge=<base64url(SHA256(code_verifier))>&code_challenge_method=S256]
```

**令牌端点**

```http
POST https://login.eveonline.com/v2/oauth/token
Host: login.eveonline.com
Content-Type: application/x-www-form-urlencoded
Authorization: Basic <base64(client_id:secret)>

grant_type=authorization_code&code=<回调里的 code>
```

**v1 → v2 的事实**：`https://login.eveonline.com/oauth/authorize` 与 `/oauth/token` 是 v1，已被弃用；v2 路径为 `/v2/oauth/authorize`、`/v2/oauth/token`。`/oauth/verify` 已移除，改为本地解码 JWT。

来源：
- https://login.eveonline.com/.well-known/oauth-authorization-server
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/migrate_v1_v2.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/sso/index.md

## A2. PKCE 与公共客户端（public / private）

- **PKCE 支持**：**支持**，且元数据里 `code_challenge_methods_supported` 只有 `S256`（不支持 `plain`）。授权请求加 `code_challenge` + `code_challenge_method=S256`；换 token 时用表单字段 `code_verifier=<原始 verifier>` 而不是 client secret。
- **公共客户端（无 secret）**：**被允许**。官方“原生应用流程”文档明确说明 PKCE 的目的就是让应用“能不带自己的 secret key 发布”（桌面/移动端无法安全保存 secret），此时 token 请求用表单字段 `client_id` 而非 HTTP Basic。
- **public / private 客户端的区别（文档层面）**：
  - “Web based application”：服务端能安全保存 secret → 用 `client_secret_basic`（HTTP Basic，`client_id:secret`）。
  - “Native / mobile / desktop”：无法保存 secret → 用 PKCE，不带 secret，token 请求 body 里放 `client_id`。
  - 元数据同时支持 `client_secret_basic` 和 `client_secret_post`（即 secret 可放 header 或 body）。
- **注册页面上是否存在显式的“public client / private client”开关**：**未核实**。官方注册文档只描述了 `Connection Type = Authentication & API Access`，没有提到客户端类型开关。

来源：
- https://login.eveonline.com/.well-known/oauth-authorization-server
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/native_sso_flow.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/web_based_sso_flow.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/sso/index.md

## A3. redirect_uri 规则（localhost / HTTPS / 端口白名单）

已核实：
- `redirect_uri` **必须与注册时填写的回调地址完全一致**，否则 SSO 直接拒绝（“If you put in any other URL other than what is defined in your SSO application the EVE SSO will reject your request”）。参数必须是 URL-encoded 的完整 URL。
- 官方文档给的开发期示例是 **`https://localhost/callback/`**（注意是 **https**，且不带端口），并强调“**绝不要把 localhost 作为已发布应用的 callback**”。

未核实 / 未找到依据：
- 是否允许 **`http://localhost:<端口>`**（纯 http、带任意端口）——**未核实**。官方文档只出现 `https://localhost/callback/`。
- 是否存在 **HTTPS 强制要求**的明文规定——**未核实**（文档只给出 https 示例 + 禁止发布应用使用 localhost，未找到“必须 https”的官方表述）。
- 是否存在 **端口白名单**——**未核实**（未找到任何端口相关说明）。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/creating_sso_application.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/web_based_sso_flow.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/native_sso_flow.md

## A4. 令牌端点返回的 JSON 字段

官方文档给出的返回体（web 与 native 流程一致）：

| 字段 | 说明 |
|---|---|
| `access_token` | **是 JWT**（文档原文：“an access token (which is a Json Web Token)”），三段式 |
| `expires_in` | 秒。文档示例为 `1199`（另一处 refresh 示例为 `1200`）→ 实际约 20 分钟 |
| `token_type` | `Bearer` |
| `refresh_token` | 长寿命字符串；不透明串（非 JWT） |

refresh 的返回体同结构（`access_token`/`token_type`/`expires_in`/`refresh_token`）。

access token 的 JWT claims（官方示例）：`scp`（scope 数组）、`jti`、`kid`、`sub`（`CHARACTER:EVE:<id>`）、`azp`（client_id）、`tenant`、`tier`、`region`、`aud`、`name`、`owner`（owner hash）、`exp`、`iat`、`iss`。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/web_based_sso_flow.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/native_sso_flow.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/refreshing_access_tokens.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/validating_eve_jwt.md

## A5. JWKS 端点与结构 + 推荐校验方式

**确切 URL：`https://login.eveonline.com/oauth/jwks`**（不是 `/v2/oauth/jwks`）。
- 实测 `GET https://login.eveonline.com/oauth/jwks`（HTTP 200）返回两组 key：
  1. `{"alg":"RS256","e":"AQAB","kid":"JWT-Signature-Key","kty":"RSA","n":"nehPQ7FQ...TeOw","use":"sig"}`
  2. `{"alg":"ES256","crv":"P-256","kid":"8878a23f-2489-4045-989e-4d2f3ec1ae1a","kty":"EC","use":"sig","x":"...","y":"..."}`
  末尾还有 `"SkipUnresolvedJsonWebKeys":true`（非标准字段，可忽略）。
- 结论：JWKS 结构含 `kid / kty / n / e`（RSA，RS256，`kid=JWT-Signature-Key`），**现在还存在一把 EC P-256 / ES256 的密钥**（旧文档说“future support for ES-256”，现已上线）。校验时建议**按 JWT 头部 `kid` 选 key**。
- 官方建议：**每次需要时先从元数据端点取 `jwks_uri`**（虽然很少变，但不保证静态），不要硬编码。
- `iss` 校验：`iss` 可能是 `login.eveonline.com` **或** `https://login.eveonline.com`（新旧文档都强调两种都要接受，其他一律拒绝）。注意实测元数据里 `issuer` 是 `https://login.eveonline.com`（无结尾斜杠），而新文档行文写 `https://login.eveonline.com/`（带斜杠）——实现上建议同时容忍有/无结尾斜杠。
- `aud` 校验：必须是**数组**，同时包含**你的 `client_id`** 和字符串 **`"EVE Online"`**，两者缺一不可。
- 还需校验 `exp`；签名算法 RS256（或 ES256）。

来源：
- https://login.eveonline.com/oauth/jwks
- https://login.eveonline.com/.well-known/oauth-authorization-server
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/validating_eve_jwt.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/sso/index.md

## A6. refresh token 有效期与刷新请求格式

**有效期（官方表述）**
- 新文档：refresh token“long-lived，可无限次换新 access token，**只要用户没有撤销该应用的授权**”。
- 旧文档：refresh token“有效期与应用未被撤销一样长”。
- 实测层面的具体秒数/天数：**未核实**（官方从未给出固定 TTL 数字）。
- **应用未发布(EUP)是否只有 72 小时**：**未核实**。我尝试抓取 CCP 官方 issue 仓库的 wiki（`https://github.com/ccpgames/sso-issues/wiki/Refresh-Tokens`，含 raw wiki 形式）均未能取到正文内容，官方文档中也没有任何“72 小时”或“未发布应用限制”的表述。**注意：官方新文档的说法与“72 小时”相冲突，请不要把 72 小时当作已核实事实。**

**刷新请求（两种形态都支持）**

Web 应用（Basic）：

```http
POST https://login.eveonline.com/v2/oauth/token
Host: login.eveonline.com
Content-Type: application/x-www-form-urlencoded
Authorization: Basic <base64(client_id:secret)>

grant_type=refresh_token&refresh_token=<URL 编码后的 refresh token>[&scope=<原 scope 的子集>]
```

原生应用（无 secret，body 里放 client_id）：

```http
POST https://login.eveonline.com/v2/oauth/token
Host: login.eveonline.com
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<...>&client_id=<你的 client_id>[&scope=<原 scope 的子集>]
```

- **`scope` 参数是可选（OPTIONAL）**：省略则新 access token 拿回全部原始 scope；给则必须是原始 scope 的子集。
- refresh token **必须 URL-encode**，否则可能 400。
- **是否返回新 refresh_token / 是否轮换**：
  - 返回体会**包含** `refresh_token` 字段。
  - 旧文档明确写：“返回的 `refresh_token` **可能与提交的不同**……未来 SSO 会为原生应用启用 refresh token 轮换，**请每次都更新本地存储的 refresh token**”。
  - 新文档未再提轮换，但同样建议始终保存最新 refresh token。**“当前是否已经在轮换”这一点我无法从文档判定 → 未核实；实践上应始终保存返回的新值。**
- 撤销：`POST https://login.eveonline.com/v2/oauth/revoke`，Basic 认证，表单体 `token_type_hint=refresh_token&token=<refresh token>`；无论 token 是否有效都返回 200。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/refreshing_access_tokens.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/sso/index.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/revoking_refresh_tokens.md
- https://login.eveonline.com/.well-known/oauth-authorization-server

## A7. 令牌请求：HTTP Basic 还是 body 里的 client_id/client_secret？

**两种都支持**（A 级证据：元数据 `token_endpoint_auth_methods_supported = ["client_secret_basic","client_secret_post","client_secret_jwt"]`）。

| 方式 | 具体形式 | 官方推荐场景 |
|---|---|---|
| `client_secret_basic` | 头 `Authorization: Basic base64(client_id:secret)`，body 只放 `grant_type`/`code` | Web 应用（服务端可保存 secret） |
| `client_secret_post` | body 里放 `client_id` + `client_secret` | 元数据声明支持；官方文档未作为主推 |
| PKCE（无 secret） | 授权阶段 `code_challenge`+`S256`；换 token 时 body 放 `client_id`+`code_verifier` | 原生/桌面/移动应用 |

来源：
- https://login.eveonline.com/.well-known/oauth-authorization-server
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/web_based_sso_flow.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/native_sso_flow.md

## A8. developers.eveonline.com 注册字段、回调规则、scope、未发布状态

**注册流程与字段（官方文档）**
- 登录 developers.eveonline.com → Applications → 接受开发者许可 → Create New Application。
- 表单字段：**Name**、**Description**、**Connection Type**（选 *Authentication & API Access* 才会出现 scope 列表）、**Permissions / Available Scopes List**（勾选所需 scope）、**Callback URL**。
- 创建完成后在 *View Application* 里查看 **client ID** 与 **secret key**，也可后续修改 scope / 回调地址。
- **必填字段的完整准确清单（例如是否强制 Description、是否允许填多个回调 URL）**：官方文档只描述上述几项，**“是否允许多个回调地址”“字段级必填性”未核实**。

**回调地址填写规则**
- 必须与授权请求里的 `redirect_uri` 完全一致，其他一律被拒。
- 开发期可用 `https://localhost/callback/`；**已发布应用绝不可用 localhost**。
- 回调地址可以事后修改。

**scope 勾选**
- 只能申请你在应用里勾选过的 scope；用户必须逐项同意。
- 强烈不建议申请全部 scope。
- **修改 scope 会作废现有 refresh token**，所有用户必须重新登录授权（官方明确警告）。

**“应用未发布(EUP)”对 token 有效期的影响**
- **未核实**：我在官方文档与 dev blog 中都没有找到“未发布/EUP 应用 refresh token 只有 72 小时”的表述；唯一相关的官方表述是 refresh token 在用户未撤销前长期有效，以及“修改 scope 会作废 refresh token”。请勿据此设计 72 小时续期逻辑而不做实测。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/sso/creating_sso_application.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/sso/index.md
- https://developers.eveonline.com/docs/services/sso/（旧版入口跳转说明）
- https://github.com/ccpgames/sso-issues/wiki/Refresh-Tokens（**抓取失败，正文不可得**）

---

# B. 需要的 ESI 端点逐条核实

约定：以下“scope / 角色 / 限流组 / 缓存 / 分页”来自 C 级镜像（官方 OpenAPI 生成，兼容日期 2026-08-18），字段则尽量用 A 级实时响应验证。

## B1–B4. 工业任务与蓝图

| 路径 | 方法 | scope | 角色 | 分页 | 缓存 | 限流组 | 主要返回字段 |
|---|---|---|---|---|---|---|---|
| `/characters/{character_id}/industry/jobs/` | GET | `esi-industry.read_character_jobs.v1` | 无 | `page`（X-Pages） | 300s | `char-industry` 600/15m | `job_id`,`installer_id`,`facility_id`,`location_id`,`activity_id`,`blueprint_id`,`blueprint_type_id`,`blueprint_location_id`,`output_location_id`,`runs`,`licensed_runs`,`successful_runs`,`status`(`active/cancelled/delivered/paused/ready/reverted`),`duration`,`start_date`,`end_date`,`pause_date`,`completed_date`,`completed_character_id`,`probability`,`product_type_id`,`cost` |
| `/corporations/{corporation_id}/industry/jobs/` | GET | `esi-industry.read_corporation_jobs.v1` | **`Factory_Manager`** | `page`（X-Pages） | 300s | `corp-industry` 600/15m | 同上（corp 版额外含 `location_id`，无 `is_completed` 便捷字段） |
| `/characters/{character_id}/blueprints/` | GET | `esi-characters.read_blueprints.v1` | 无 | `page`（X-Pages） | 3600s | `char-industry` 600/15m | `item_id`,`type_id`,`location_id`,`location_flag`,`quantity`,`time_efficiency`,`material_efficiency`,`runs` |
| `/corporations/{corporation_id}/blueprints/` | GET | `esi-corporations.read_blueprints.v1` | **`Director`** | `page`（X-Pages） | 3600s | `corp-industry` 600/15m | 同上 |

补充（两个 industry jobs 路由都支持的可选查询参数）：`include_completed`（bool，可选）、`page`（int）。
`quantity` 语义：`-1`=原图(BPO)，`-2`=蓝图拷贝(BPC)，正整数=未使用过的原图堆叠。`runs`：拷贝为剩余次数，原图为 `-1`。
`location_flag` 是枚举（`Hangar`、`Cargo`、`CorpSAG1..7`、`AssetSafety` 等）。

来源：
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/c6b9839a7b866ce7dc7bf9a1eeb1337be0eb01bd （角色 industry jobs 操作定义）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/160cb4fa2c110633ec68a0a0345b5b4c3f536b1a （军团 industry jobs 操作定义）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/4c3a836b8744f5c17b31cc201db794465be018bb （角色 blueprints）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/f35128f3ba43f775aeae16a608f2bb46ac517b64 （军团 blueprints）
- 字段清单亦见于 https://esi.evetech.net/meta/openapi.json 的 `CharactersCharacterIdIndustryJobsGet` / `CorporationsCorporationIdIndustryJobsGet` / `CharactersCharacterIdBlueprintsGet` schema（我抓到了该文件的前 ~100 KB，含这些 schema）

## B5. `GET /industry/systems/`（系统成本指数）

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | 无（公共） |
| 角色 | 无 |
| 分页 | 无（一次性返回全部工业星系） |
| 缓存 | 3600s |
| 限流组 | `industry` 150/15m |

**实测返回结构（A 级）**：数组，每项：

- `solar_system_id`（int）
- `cost_indices`：数组，每项 `{ "activity": <string>, "cost_index": <number> }`

`activity` 的取值集合（实测全量出现）：**`manufacturing`、`researching_time_efficiency`、`researching_material_efficiency`、`copying`、`invention`、`reaction`**（共 6 个）。
`cost_index` 是**系数而非百分比**（例如 Jita 所在 30000142 = 0.1665 表示 16.65%；最低值 0.0014 表示 0.14%）。每条路由的 `cost_index` 与该星系被使用的“工业产能”相关。

来源：
- https://esi.evetech.net/latest/industry/systems/ （实测；返回中可见 `{"activity":"reaction","cost_index":0.0014}` 与 `solar_system_id:30000142` = 0.1665）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/776abf9c1503a28ef400009e10fb5a6583bc3cf7

## B6. `GET /industry/facilities/`

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | 无（公共） |
| 角色 | 无 |
| 分页 | 无 |
| 缓存 | 3600s |
| 限流组 | `industry` 150/15m |

返回字段（来自同一生成规范的 DTO）：

| 字段 | 类型 | 必需 |
|---|---|---|
| `facility_id` | int | 是 |
| `owner_id` | int | 是 |
| `region_id` | int | 是 |
| `solar_system_id` | int | 是 |
| `type_id` | int | 是 |
| `tax` | float | 否（可为 null/缺省） |

来源：
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/311c62ac314a5e721a058c4314088d87fd3e80a9 （操作定义）
- https://api.github.com/repos/seatplus/esi-schema/contents/src/Responses/IndustryFacilitiesGetItem.php （字段定义）

## B7. `GET /markets/prices/`

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | 无（公共） |
| 角色 | 无 |
| 参数 | **无**（该操作不接受任何请求参数，包括 `type_id`；返回全量） |
| 分页 | 无 |
| 缓存 | 3600s |
| 限流组 | 无（未纳入新限流，受旧 error limit 约束） |

**实测返回（A 级）**：数组，每项字段为 `type_id`、`adjusted_price`、`average_price`。实测样例：
`{"adjusted_price":28.188408743393072,"average_price":25.27,"type_id":18}`

⚠️ **重要实现细节（实测发现）**：`average_price` **可能不存在**，例如实测出现 `{"adjusted_price":30.624744996084605,"type_id":265}`、`{"adjusted_price":0.0,"type_id":3279}`。解析时必须把 `average_price` 当作可选字段。
`adjusted_price` 可为 `0.0`（表示无调整价）。

来源：
- https://esi.evetech.net/markets/prices/ （实测）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/c103f26274dbf28f9c5a5502976b8483ba7e2188

## B8. `GET /markets/{region_id}/orders/`

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | 无（公共） |
| 角色 | 无 |
| 参数 | `order_type`（`buy`/`sell`/`all`，必需）、`page`（int，可选，默认 1）、`type_id`（int，可选） |
| 分页 | **`page` + `X-Pages` 响应头**（offset 分页；官方明确说现有路由仍用 offset，新路由才用 cursor） |
| 缓存 | **300s（5 分钟）** |
| 限流组 | **`market-order` 12000/15m**（2026-02-24 生效，该组**只有这一条路由**） |

**`The Forge` 的 region_id = `10000002`**（A 级实测）。

**实测返回字段（A 级，`type_id=34&order_type=sell`）**：`order_id`,`type_id`,`location_id`,`system_id`,`price`,`is_buy_order`,`issued`,`duration`,`min_volume`,`range`(`region` 等),`volume_remain`,`volume_total`。
注意：`order_type=sell` 时返回的 `is_buy_order` 为 `false`；`range` 取值形如 `region`/`station`/`solarsystem`/`1`..`40`。

来源：
- https://esi.evetech.net/markets/10000002/orders/?type_id=34&order_type=sell （实测）
- https://esi.evetech.net/universe/regions/10000002/ （实测返回 `"name":"The Forge"`）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/2df40a827b8f725a52d1dd7b2a72be337ab56ff3
- https://developers.eveonline.com/blog/market-orders-rate-limit-rolls-out-on-february-24-2026 （12000 tokens、5 分钟缓存、113 个 region、共 1723 页）
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/pagination/x-pages.md

## B9. `GET /markets/{region_id}/history/`

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | 无（公共） |
| 角色 | 无 |
| 参数 | `type_id`（int，**必需**） |
| 分页 | **无**（按天返回全部历史，通常约 400 天） |
| 缓存 | 该操作在生成规范中 `CACHE_AGE = null`（未标注缓存）；实测 `Last-Modified`/`Expires` 未能观察（我的抓取工具不返回响应头）→ **缓存语义未核实** |
| 限流组 | 无 |

**实测返回字段（A 级）**：`date`（`YYYY-MM-DD`）、`average`、`highest`、`lowest`、`order_count`、`volume`。
实测样例：`{"average":3.84,"date":"2025-09-01","highest":3.89,"lowest":3.81,"order_count":2094,"volume":4657183484}`（数据覆盖到 2026-10-11）。

来源：
- https://esi.evetech.net/markets/10000002/history/?type_id=34 （实测）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/5066cb6949ce5777b0dd6291679ebd761716d3e7

## B10. `GET /characters/{id}/assets/` 与 `GET /corporations/{id}/assets/`

| 路径 | 方法 | scope | 角色 | 分页 | 缓存 | 限流组 |
|---|---|---|---|---|---|---|
| `/characters/{character_id}/assets/` | GET | `esi-assets.read_assets.v1` | 无 | `page`（X-Pages） | 3600s | `char-asset` 1800/15m |
| `/corporations/{corporation_id}/assets/` | GET | `esi-assets.read_corporation_assets.v1` | **`Director`** | `page`（X-Pages） | 3600s | `corp-asset` 1800/15m |

返回字段（两个路由相同，来自官方 OpenAPI 的 `CharactersCharacterIdAssetsGet` schema，我已抓到）：

`item_id`,`type_id`,`quantity`,`location_id`,`location_type`(`station`/`solar_system`/`item`/`other`),`location_flag`,`is_singleton`,`is_blueprint_copy`。
必需字段：`type_id`,`quantity`,`location_id`,`location_type`,`item_id`,`location_flag`,`is_singleton`。

要点：
- 军团资产的 `location_flag` 枚举包含 `CorpSAG1..CorpSAG7`、`OfficeFolder`、`CorpDeliveries` 等（角色版没有 SAG；`AssetSafety` 两者都有）。
- `location_id` 是**不透明 ID**：可能是空间站、建筑、星系，或**另一个 item_id（集装箱）**。
- **单页最多 1000 条**（`page` 参数 + `X-Pages` 头翻页）——**“每页 1000 条”这一点我未在本次抓取的官方文档中直接读到 → 未核实**（但 X-Pages/page 分页方式是已核实的）。

来源：
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/67cf9c192ca5690def244d4887dd24db91463716
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/dc33af57a924320222c3fb600cd1972e8e6fd23e
- https://esi.evetech.net/meta/openapi.json （`CharactersCharacterIdAssetsGet` schema，含 `location_flag` 全枚举）
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/pagination/x-pages.md

## B11. `POST /universe/names/`

| 项 | 值 |
|---|---|
| 方法 | **POST**（用 GET 访问无效；实测 `GET /universe/names/` 未返回可用 JSON，工具报 “unsupported content type”） |
| scope | 无（公共） |
| 角色 | 无 |
| 分页 | 无 |
| 缓存 | 未标注（生成规范中 `CACHE_AGE = null`） |
| 请求体 | **裸数组**（不是对象包一层）：`[<id1>, <id2>, ...]`，`Content-Type: application/json` |
| 单次最多 id 数 | **未核实**（生成规范未暴露 `maxItems`；广为流传的 1000 上限我这次没有抓到官方依据） |
| 返回结构 | 数组，每项 `{ "category": <string>, "id": <int>, "name": <string> }` |

`category` 取值形如 `character`/`corporation`/`alliance`/`faction`/`constellation`/`solar_system`/`region`/`station`/`type`/`inventory_type` 等（**完整枚举未核实**）。

来源：
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/568ac22c0105a8ed82e4472047224494fd4da834 （操作定义：`invoke('POST','/universe/names', [], [], (array)$requestBody)`）
- https://api.github.com/repos/seatplus/esi-schema/contents/src/Responses/UniverseNamesPostItem.php （`category`,`id`,`name`）
- https://esi.evetech.net/universe/names/ （GET 实测，未得到 JSON）

## B12. `GET /universe/types/{type_id}/`

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | 无（公共） |
| 角色 | 无 |
| 分页 | 无 |
| 缓存 | 未标注（`CACHE_AGE = null`） |

**是否包含 `volume` / `portion_size` / `packaged_volume`：包含，三者都有。** 实测 `GET /universe/types/34/`（Tritanium）返回：
`capacity`,`description`,`group_id`,`icon_id`,`market_group_id`,`mass`,`name`,`packaged_volume`(0.01),`portion_size`(1),`published`,`radius`,`type_id`,`volume`(0.01)。
注意：`capacity`/`mass`/`volume`/`packaged_volume` 都是 float，`portion_size` 是 int；`market_group_id`、`icon_id` 等可能缺省（**可选性未逐字段核实**）。

来源：
- https://esi.evetech.net/universe/types/34/ （实测）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/a74488c079a5f34978f8195675610f8dce0db4f6

## B13. `GET /universe/structures/{structure_id}/`

| 项 | 值 |
|---|---|
| 方法 | GET |
| scope | **`esi-universe.read_structures.v1`** |
| 角色 | 无 |
| 分页 | 无 |
| 缓存 | 3600s |

**失败条件（实测）**：
- **不带 token → HTTP 401**，报文实测为 `{"error":"Unauthorized - No token provided"}`（我实测的正是 `GET /universe/structures/1/`）。
- **403 的具体条件与报文：未核实**。官方语义是：token 缺 `esi-universe.read_structures.v1`，或该角色对该建筑没有访问/停靠权限时返回 403（我没有可用 token，无法实测 403 的报文）。
- 另外：用 `location_id` 拿到建筑 ID 后**必须**用这个带 scope 的端点解析名字，不能靠搜索接口“发现”建筑（官方规则明确禁止把 ESI 当发现工具，滥用会被封禁）。

来源：
- https://esi.evetech.net/universe/structures/1/ （实测 401）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/9853f989d09a80a42f0c038286a41e8917f2d0f8
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/esi_introduction.md （Discovery 规则）

## B14. `GET /universe/stations/{station_id}/` 与 `GET /universe/systems/{system_id}/`

| 路径 | 方法 | scope | 角色 | 分页 | 主要字段（实测） |
|---|---|---|---|---|---|
| `/universe/stations/{station_id}/` | GET | 无（公共） | 无 | 无 | `station_id`,`name`,`type_id`,`owner`,`system_id`,`position{x,y,z}`,`race_id`,`max_dockable_ship_volume`,`office_rental_cost`,`reprocessing_efficiency`,`reprocessing_stations_take`,`services[]` |
| `/universe/systems/{system_id}/` | GET | 无（公共） | 无 | 无 | `system_id`,`name`,`constellation_id`,`position{x,y,z}`,`security_status`,`security_class`,`star_id`,`planets[]`（每项 `planet_id`，部分含 `moons[]`）,`stargates[]`,`stations[]` |

实测例：
- `GET /universe/stations/60003760/` → `{"name":"Jita IV - Moon 4 - Caldari Navy Assembly Plant", "system_id":30000142, ...}`
- `GET /universe/systems/30000142/` → `{"name":"Jita","security_status":0.9459131360054016,"constellation_id":20000020, ...}`

来源：
- https://esi.evetech.net/universe/stations/60003760/ （实测）
- https://esi.evetech.net/universe/systems/30000142/ （实测）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/3e864b9cce96e82badbec2dbb4c47d3b8d8cfb85
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/62c63c5317cf624c2174effba08d2d485050564d

## B15. 蓝图的制造材料清单：ESI 有公开端点吗？

**明确回答：没有。** ESI **不存在**任何公开端点能返回某个 blueprint type 的制造材料（Manifest/Materials）清单。工业相关的公开端点只有 `/industry/systems/` 与 `/industry/facilities/`（成本指数与设施列表），角色/军团端点只返回**你自己已有的蓝图与任务**（`/characters/{id}/blueprints/`、`/corporations/{id}/blueprints/`、`industry/jobs`），都不含配方法表。

**通常获取方式：SDE（Static Data Export）**，官方下载入口 `https://developers.eveonline.com/static-data/`，格式为 JSON Lines 或 YAML，自动化可用 `latest.jsonl` 取最新 build number，再下载 `eve-online-static-data-<build>-<variant>.zip`；SDE 有 schema changelog 与增量 changes 文件。

**我未核实的部分**：SDE 中承载配方的**具体文件名/表名**（例如 `blueprints.yaml` 的字段结构）——本次未抓取 SDE 内容，**未核实**；我只能确认 SDE 是官方静态数据出口，而 ESI 没有相应端点。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/static-data/index.md
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/34873696d6854ef5c67539324fc569d3d6fd148f （全 API 仅 233 条路由，工业类只有上述几条）
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/endpoints.md （“所有端点都在 API Explorer”）

## B16. 军团工业 job 的角色精确写法与缺角色时的状态码

**角色名（官方 OpenAPI 的 role 枚举，我已抓到该枚举全文）——精确大小写与下划线如下**：
`Director`、`Factory_Manager`；同一枚举里相关的还有 `Accountant`、`Junior_Accountant`、`Personnel_Manager`、`Project_Manager`、`Rent_Factory_Facility`、`Rent_Research_Facility`、`Station_Manager`、`Trader`、`Auditor`、`Contract_Manager` 等（共 60 项左右，全部为 `PascalCase` + 下划线分段）。

**本次所需端点的角色要求**：
- `/corporations/{corporation_id}/industry/jobs/` → `Factory_Manager`
- `/corporations/{corporation_id}/blueprints/` → `Director`
- `/corporations/{corporation_id}/assets/` → `Director`

**缺角色时的状态码**：
- **不带 token → 401**（实测报文 `{"error":"Unauthorized - No token provided"}`）。
- **带 token 但缺 scope / 缺角色 → 403**（官方与生态一致的行为；**403 的具体报文文本我未实测核实**）。
- 缺 scope 与缺角色在 ESI 上都归入 403，是两回事：缺 scope 需重新授权（改 scope 会作废 refresh token），缺角色需在游戏内给该角色授予军团职位。

来源：
- https://esi.evetech.net/meta/openapi.json （`CorporationsCorporationIdRolesGet` 的 `grantable_roles` 枚举，含 `Director`、`Factory_Manager`）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/160cb4fa2c110633ec68a0a0345b5b4c3f536b1a （`REQUIRED_ROLES = ['Factory_Manager']`）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/f35128f3ba43f775aeae16a608f2bb46ac517b64 （`REQUIRED_ROLES = ['Director']`）
- https://api.github.com/repos/seatplus/esi-schema/git/blobs/dc33af57a924320222c3fb600cd1972e8e6fd23e （`REQUIRED_ROLES = ['Director']`）
- https://esi.evetech.net/universe/structures/1/ （401 实测）

---

# C. ESI 通用约定

## C1. datasource 参数、兼容日期头的现状与边界

**X-Compatibility-Date（现状，已核实）**
- 每个请求都可以带 `X-Compatibility-Date: YYYY-MM-DD`；无法设置头的客户端（如浏览器 fetch）可用查询参数 **`compatibility_date=YYYY-MM-DD`**，效果相同。
- **不发送时使用“最旧的可用兼容日期”**。实测：`GET /status/?compatibility_date=2019-01-01` → 400 `{"error":"Compatibility date (2019-01-01) is before the oldest supported date: 2020-01-01."}` → **最旧 = `2020-01-01`**。
- 未来日期被拒：`?compatibility_date=2026-10-13` → 400 `{"error":"Compatibility date (2026-10-13) is in the future. Current date (UTC-11) is 2026-10-12."}`。因此**“API 的今天”在 11:00 UTC 翻页，最新可用日期 = 当前日期(UTC-11)**，实测接受 `2026-10-12`（返回 200）。
- 响应头 `X-Compatibility-Date` 会告诉你实际匹配到的版本（≤ 你请求的日期）。
- 版本策略已从“每路由 `/vN/`”改为“整个应用一个兼容日期”：`/v1/status/` → `/status/`；`dev`/`latest`/`legacy` 已合并。**旧版本化 URL 仍可用（“for the foreseeable future”）**，但新路由只支持兼容日期。
- **“最新兼容日期”的确切值**：官方没有给出枚举；我只能确认**上界 = 当天(UTC-11)**，实测 `2026-10-12` 可用。另有第三方生成的 SDK 以 `2026-08-18` 为生成基准（说明 8-18 也是一个可用日期）。→ **“最新有行为变更的日期编号”未核实**。

**datasource 参数（现状）**
- 旧文档（已过时）把 `datasource` 描述为多租户开关，可在任意路由上切换，并存在 esi-dev/esi-test/生产三套环境。
- **现状（部分核实）**：我在最新 OpenAPI 的共享 `components.parameters` 中**没有看到 `datasource` 查询参数**，取而代之的是 **`X-Tenant` 头（`default: tranquility`）**；官方也有 dev blog《Removing datasource singularity》。→ **“datasource 是否仍被接受”未核实**（建议只用 `X-Tenant` 头或省略，走默认 tranquility）。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/overview.md
- https://esi.evetech.net/status/?compatibility_date=2026-10-13 （400，含“当前日期”）
- https://esi.evetech.net/status/?compatibility_date=2019-01-01 （400，最旧日期）
- https://esi.evetech.net/status/?compatibility_date=2026-10-12 （200）
- https://developers.eveonline.com/blog/changing-versions-v42-was-getting-out-of-hand
- https://esi.evetech.net/meta/openapi.json （`components.parameters` 仅有 `AcceptLanguage`/`CompatibilityDate`/`IfModifiedSince`/`IfNoneMatch`/`Tenant`）
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/esi_introduction.md （旧的 datasource 多租户描述）
- https://developers.eveonline.com/blog/removing-datasource-singularity （见 sitemap 列表）

## C2. 速率限制策略（420 / 429 / Retry-After / error limit / 并发）

**新式限流：浮动窗口 + 令牌桶（已核实）**
- 按 `rate limit group` × `userID` 分配桶。`userID`：已认证路由为 `<applicationID>:<characterID>`；未认证为 `<sourceIP>`（若带 token 则为 `<sourceIP>:<applicationID>`）。
- **令牌消耗**：2XX = 2 令牌；3XX = 1；4XX = 5（**429 本身除外**）；5XX = 0。
- **超限 → 429**，并返回 `Retry-After`（秒）。
- 响应头：`X-Ratelimit-Group`、`X-Ratelimit-Limit`（形如 `150/15m`）、`X-Ratelimit-Remaining`、`X-Ratelimit-Used`；被限流时再加 `Retry-After`。
- 规范里用 `x-rate-limit` 扩展声明 `group`/`window-size`/`max-tokens`。
- **并非所有路由都已启用**；且**部分路由在 EVE 服务端还有更深的限流器，会返回不带限流头的 429**（官方承认，正在逐步废弃）。
- 官方建议（非硬性）：不要贴着上限跑；`X-Ratelimit-Remaining` 接近 0 就降速；把请求摊开而不是持续突发；定时任务用“上次任务结束后 N 分钟”而不是 `*/5` cron。

**旧式 error limit（已核实，且与新限流头互斥）**
- 对没有启用新限流的路由仍生效：**每分钟最多 100 个非 2xx/3xx 响应**，超限后**所有** ESI 路由都返回 **420**（即使该路由启用了新限流）。
- 响应头：`X-ESI-Error-Limit-Remain`（本时间窗剩余错误数）、`X-ESI-Error-Limit-Reset`（距离重置的秒数）。
- 触发 error limit 后所有请求会被丢弃直到窗口结束；不遵守可能导致封禁（按 IP 的永久封禁）。

**关于 420 与 429 的分工**：420 = error limit（错误太多）；429 = 令牌桶/new 限流超限。二者都可能带 `Retry-After`（429 明确有；420 我**未核实**是否带）。

**建议并发数**：**未核实**。官方文档没有给出任何“建议并发请求数”的数字，只给出“不要贴着限流跑、把负载摊平、尊重缓存”的定性建议。若需要具体并发，请按各路由的 `X-Ratelimit-Limit`（例如 `market-order` 12000/15m、`char-asset` 1800/15m、`industry` 150/15m）与缓存 TTL 自行推算。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/rate-limiting.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/best-practices.md
- https://raw.githubusercontent.com/esi/esi-docs/master/docs/esi_introduction.md
- https://developers.eveonline.com/blog/market-orders-rate-limit-rolls-out-on-february-24-2026

## C3. ETag / If-None-Match / Expires 缓存机制与 304

- `Expires` = ESI 缓存**该资源何时过期**，即何时才有新数据。**不应在它之前更新**；提前拉取最好情况是拿到缓存（浪费双边资源），最坏情况可能被视为“绕过 ESI 缓存”而被封禁。
- `Last-Modified` = 缓存中数据最后一次更新（**建议用 `If-Modified-Since` 配合**）。
- `ETag` = 内容哈希。下一次请求带 `If-None-Match: <上次的 ETag>`；若内容未变，服务器返回 **304 Not Modified**，表示“无需处理新数据”。
- **304 的含义**：数据未变，请复用本地副本；在令牌体系里 3XX 只花 1 个令牌（2XX 花 2 个），所以带条件请求更省额度。
- 分页注意：**同一资源的所有页 `Last-Modified` 应相同**，可用它校验“翻页过程中缓存是否刷新”。
- 静态数据（行星、卫星、type 等）应返回一致的共享缓存头。
- 部分资源（尤其 POST）不提供缓存头，即使内部仍有缓存。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/best-practices.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/rate-limiting.md

## C4. 分页：`X-Pages` 与 `page`

- **offset 分页（现有绝大多数列表路由）**：请求参数 `page`（**从 1 开始**），响应头 **`X-Pages`** 给出总页数；按 1..X-Pages 顺序抓取。
- 已知陷阱：翻页期间缓存过期会导致重复/漏数据。官方建议：若第 1 页快到 `Expires`，先等缓存刷新再一次性抓完全部页；并用 `Last-Modified` 校验一致性。
- **cursor 分页（新路由）**：`limit`、`before`、`after` 三个参数，返回体里带 `cursor: {before, after}` 对象；记录按“最后修改时间”排序；`after` 返回更新的记录，`before` 返回更旧的；**只有拿到空列表才算到达数据集端点**；token 是不透明字符串，不要解析/校验/缓存以外的加工；同一记录可能重复出现（正常）。官方明确：**现有路由不会改造，仍是 offset 分页**，新路由才用 cursor。
- 另有 from-id 分页（本报告未使用）。

来源：
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/pagination/x-pages.md
- https://raw.githubusercontent.com/esi/esi-docs/main/docs/services/esi/pagination/cursor-based.md
- https://developers.eveonline.com/blog/changing-pagination-turning-a-new-page

## C5. base URL 与最新兼容日期推荐

- **`https://esi.evetech.net/latest` 仍然可用**，但已不是推荐写法。官方迁移指引：去掉所有版本前缀（`/v1/status/` → `/status/`），因为 `dev`/`latest`/`legacy` 已合并。**推荐：`https://esi.evetech.net`（无前缀）+ 每个请求带 `X-Compatibility-Date`。**
- 我在本次核实中**两种形式都实测成功**（例如 `https://esi.evetech.net/latest/industry/systems/` 与 `https://esi.evetech.net/universe/types/34/` 均 200）。
- **旧的 `swagger.json` 已不存在**：`https://esi.evetech.net/latest/swagger.json` 与 `https://esi.evetech.net/latest/openapi.json` 均返回 **404**。规范改在 **`https://esi.evetech.net/meta/openapi.json`（OpenAPI 3.1）** 与 **`https://esi.evetech.net/meta/openapi-3.0.json`（OpenAPI 3.0）**，两者我都实测 200。Swagger 2.0 规范只读保留一段时间后删除；新路由只进 OpenAPI。
- **“推荐用最新兼容日期”**：官方建议**始终发送 `X-Compatibility-Date`**（不方便设头时用 `compatibility_date` 查询参数），并可在升级时统一改这一个值。**不要发送未来日期**；需要“今天”时用 `now() - 11h` 得到 API 的当前日期（本次为 `2026-10-12`）。
- 查看规范/试调：`https://developers.eveonline.com/api-explorer`（我实测可达，但是 JS 单页应用，静态抓取拿不到内容）。

来源：
- https://developers.eveonline.com/blog/changing-versions-v42-was-getting-out-of-hand
- https://developers.eveonline.com/blog/changing-specs-from-swagger-to-openapi
- https://esi.evetech.net/meta/openapi.json
- https://esi.evetech.net/meta/openapi-3.0.json
- https://esi.evetech.net/latest/swagger.json （404 实测）
- https://esi.evetech.net/latest/openapi.json （404 实测）
- https://esi.evetech.net/latest/industry/systems/ 与 https://esi.evetech.net/universe/types/34/ （两种 base 形式均实测可用）
- https://developers.eveonline.com/api-explorer

---

# D. 我无法核实的点（明确清单）

| 编号 | 未核实内容 | 原因 / 现状 |
|---|---|---|
| A3 | 是否允许 `http://localhost:<端口>`；是否强制 HTTPS；是否有端口白名单 | 官方文档只给 `https://localhost/callback/` 示例 + “发布应用勿用 localhost”；未找到端口/协议白名单说明 |
| A2/A8 | 注册页是否存在显式 public/private 客户端类型开关 | 注册文档只描述 Name/Description/Connection Type/Callback/Scopes |
| A6/A8 | **refresh token 是否在未发布(EUP)应用上只有 72 小时** | 官方文档无此表述；CCP wiki `ccpgames/sso-issues/wiki/Refresh-Tokens` 多次抓取失败（含 raw wiki），正文不可得；官方新文档反而说 refresh token 长期有效 |
| A6 | refresh token 的确切 TTL；轮换是否已生效 | 官方从未给数字；轮换旧文档说“未来会启用”，新文档未提 |
| A8 | 是否允许多个回调地址、字段级必填性 | 文档未说明 |
| B8/B10 | `X-Pages` / `Retry-After` 等**响应头的实际取值** | 我的抓取工具只返回响应体，不返回响应头；分页与限流头行为依据官方文档 |
| B9 | `/markets/{region_id}/history/` 的缓存语义 | 生成规范标 `CACHE_AGE = null`；响应头不可观察 |
| B10 | “资产每页 1000 条”这一具体数字 | 未在官方文档中读到 |
| B11 | `POST /universe/names/` **单次最多 id 数**（常说的 1000）；`category` 完整枚举 | 生成规范未暴露 `maxItems`；且我的工具无法发 POST |
| B13 | `/universe/structures/{id}/` **403 的具体条件与报文** | 我没有可用 token；仅实测了无 token 的 401 |
| B15 | SDE 中承载蓝图材料的具体文件名/字段结构 | 本次未抓取 SDE 内容 |
| B16 | 缺 scope/角色时 403 的**确切报文文本** | 无 token，无法触发 403 |
| C1 | **“最新有行为变更的兼容日期”的确切值**；`datasource` 参数是否仍被接受 | 只能证明上界 = 当天(2026-10-12)、下界 = 2020-01-01；新规范里改为 `X-Tenant` 头 |
| C2 | 420 是否带 `Retry-After`；**官方建议的并发数** | 官方未给出并发建议；420 的头未实测 |
| 全局 | 官方 OpenAPI 的 `paths` 段（逐路由的官方 scope/roles 原文） | 规范文件过大（抓取截断），且 shell 出网被沙箱阻断无法下载后本地检索；已用“官方 OpenAPI 生成的 SDK（记录了兼容日期 2026-08-18）”替代，并与官方文档 + 实时接口交叉验证 |

---

# E. 供直接照抄的 SSO 速查

```http
# 1) 授权（Web，有 secret）
GET https://login.eveonline.com/v2/oauth/authorize?response_type=code&redirect_uri=<URLENCODED_CALLBACK>&client_id=<CLIENT_ID>&scope=<URLENCODED_SCOPE_LIST>&state=<RANDOM>

# 2) 授权（原生/桌面，PKCE，无 secret）
GET https://login.eveonline.com/v2/oauth/authorize?response_type=code&redirect_uri=<URLENCODED_CALLBACK>&client_id=<CLIENT_ID>&scope=<URLENCODED_SCOPE_LIST>&state=<RANDOM>&code_challenge=<BASE64URL_SHA256_VERIFIER>&code_challenge_method=S256

# 3) 换 token（Web：Basic 认证，表单体）
POST https://login.eveonline.com/v2/oauth/token
Content-Type: application/x-www-form-urlencoded
Authorization: Basic <BASE64(CLIENT_ID:SECRET)>
grant_type=authorization_code&code=<CODE>

# 4) 换 token（原生：body 带 client_id + code_verifier）
POST https://login.eveonline.com/v2/oauth/token
Content-Type: application/x-www-form-urlencoded
grant_type=authorization_code&code=<CODE>&client_id=<CLIENT_ID>&code_verifier=<VERIFIER>

# 5) 刷新（Web）  6) 刷新（原生，body 加 client_id）
POST https://login.eveonline.com/v2/oauth/token
grant_type=refresh_token&refresh_token=<URLENCODED_RT>[&scope=<SUBSET>]

# 7) 撤销  8) 元数据  9) JWKS  10) 校验用 metadata
POST https://login.eveonline.com/v2/oauth/revoke     （Basic + token_type_hint=refresh_token&token=<RT>）
GET  https://login.eveonline.com/.well-known/oauth-authorization-server
GET  https://login.eveonline.com/oauth/jwks
```

**JWT 校验清单**：签名用 JWKS 中与头部 `kid` 匹配的 key（当前有 `JWT-Signature-Key`(RS256/RSA) 与一把 EC P-256/ES256 key）；`iss` ∈ {`login.eveonline.com`, `https://login.eveonline.com`(可容忍结尾斜杠)}；`aud` 必须**同时**含你的 `client_id` 与 `"EVE Online"`；检查 `exp`；用 `sub`（`CHARACTER:EVE:<id>`）取角色 ID，`name` 取名字，`scp` 取已授权 scope。
