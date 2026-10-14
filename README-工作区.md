# EVE 工业计算器 · 网页版本地版（开发工作区）

**基线：v0.8.16 网页版**（本地 HTTP 服务 + 浏览器界面）。

对应仓库：https://github.com/zhp8059/eve-industry-calculator-web

## 怎么跑（开发方式）

```powershell
# 需要完整 Python（3.11+，标准库即可，无第三方依赖）
python -m app.main              # 启动本地服务并自动打开浏览器
python -m app.main --port 8420  # 指定端口
```

数据目录 `data/`、`config.json` 会在首次运行时自动创建（未包含在本工作区里）。

## 怎么测

```powershell
python -m unittest discover -s tests        # 内核单元测试（156 项）
node tests/ui_render_test.js                # 前端渲染用例（需 Node）
python tests/acceptance.py <端口>            # 接口验收（需先启动服务）
python tools/verify_scopes.py               # 核对 ESI/SSO scope 与官方 spec
```

## 目录

| 路径 | 作用 |
|---|---|
| `app/*.py` | 内核：ESI / SSO / SDE / SQLite / 工业·研究·市场·规划算法 |
| `app/web/` | 前端：`app.js` / `index.html` / `style.css`（零第三方依赖） |
| `app/server.py` | 本地 HTTP 服务与接口路由 |
| `docs/` | 公式与数据来源核实、ESI/SSO 核实报告、令牌与登录安全 |
| `tests/` | 单元测试 + 前端渲染用例 + 接口验收 |
| `tools/` | 便携包构建、scope 校验 |

## 注意

* 本工作区**不含** `data/`（数据库与令牌）与 `runtime/`（90MB 便携运行时）——
  前者因机器相关且含密钥，后者可用 `tools/build_portable.py` 重新生成。
* 若要与"便携版"行为一致地验证，可从 Releases 下载 zip 解压运行。
