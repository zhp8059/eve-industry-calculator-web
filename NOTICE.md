# 第三方声明 / Third-party notices

## EVE Online

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights
are reserved worldwide. All other trademarks are the property of their respective
owners.

EVE Online, the EVE logo, EVE and all associated logos and designs are the
intellectual property of CCP hf. All artwork, screenshots, characters, vehicles,
storylines, world facts or other recognizable features of the intellectual
property relating to these trademarks are likewise the intellectual property of
CCP hf.

CCP hf has granted permission to use EVE Online and all associated logos and
designs for promotional and information purposes on its website but does not
endorse, and is not in any way affiliated with, this project. CCP is in no way
responsible for the content on or functioning of this project, nor can it be
liable for any damage arising from use of this project.

本项目是**非官方第三方工具**，与 CCP hf 无任何隶属或授权关系。

## 数据来源

程序运行时访问以下官方服务，它们各自的数据与内容归 CCP hf 所有：

| 服务 | 地址 | 用途 |
|---|---|---|
| EVE SSO | `login.eveonline.com` | 账号授权（OAuth 2.0 + PKCE） |
| ESI | `esi.evetech.net` | 工业项目、蓝图、库存、市场、成本指数等 |
| 静态数据（SDE） | `developers.eveonline.com/static-data/` | 物品、蓝图配方、建筑加成属性 |
| 图片服务器 | `images.evetech.net` | 物品图标 |

## 参考与交叉校验（不参与运行）

开发过程中用于核对公式与数据的公开来源：

- <https://developers.eveonline.com/> — ESI 开发者门户
- <https://github.com/esi/esi-docs> — ESI 官方文档
- <https://ref-data.everef.net/> — EVE Ref 参考数据（用于交叉校验 SDE 数据）
- <https://sde.hoboleaks.space/> — Hoboleaks SDE（用于交叉校验活动 ID）
- <https://wiki.eveuniversity.org/Manufacturing> — 社区公式说明

## Python 运行时（仅便携版）

便携版内置了 Python 官方的 **embeddable package**，来源：

| 项目 | 内容 |
|---|---|
| 地址 | `https://www.python.org/ftp/python/<版本>/python-<版本>-embed-amd64.zip` |
| 默认版本 | Python 3.13.16（可用 `打包便携版.bat --version X.Y.Z` 或 `--latest` 更换） |
| 许可证 | **Python Software Foundation License Version 2**（PSF-2.0），随包附有 `runtime/LICENSE.txt` |
| 修改 | 仅修改了 `python3xx._pth`：追加一行 `..`，使解释器能导入本项目自己的 `app` 包 |

Python 是 Python Software Foundation 的商标。本项目与 PSF 无隶属关系。
PSF 许可证允许自由再分发，前提是保留其许可证文本（本便携包已包含）。

## 本项目自身

本项目的源代码以 MIT 许可证发布，见 [LICENSE](LICENSE)。
