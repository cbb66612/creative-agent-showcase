# 灵感引擎 · 完整应用

这里保留原始系统的 Python 标准库服务端功能：账号与权限、资源浏览与搜索、Skill ZIP 上传和下载、版本记录、预设维护、统计、AI 助手及内置 Agent。公开资源快照排除了 Flow 平台的 75 个 Skill、两条关联 TV Skill 及相关文件；程序中的功能路由没有删减。

## 本地启动

1. 在仓库根目录运行 `python scripts/restore_public_data.py`。该脚本把公开快照复制到本机 `server/data/`，不会从外部同步任何内容。
2. 运行 `python server/app.py`，默认打开 <http://127.0.0.1:8000>。Windows 也可双击本目录的 `start.bat`，使用端口 8012。
3. 首次启动会在本机 `server/data/初始账号.txt` 生成随机管理员密码。账号为 `demo_admin`。

Agent 和 AI 问答需要在管理员页面配置自己的模型 API Key。Key 只写入本机 `server/data/config.json`，不属于公开仓库。未配置 Key 时，资源浏览、搜索、上传、下载和管理功能仍可使用。

对外部署需要能运行 Python 并保存 SQLite 与上传文件的服务器；GitHub Pages 只能展示仓库根目录的静态介绍页，不能运行内置 Agent。
