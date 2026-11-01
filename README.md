# 灵感引擎 · Skill、预设与 Agent 项目展示

麦香冰制作的个人项目展示，包含完整的 Python 应用和一页静态介绍。应用保留账号与权限、资源管理、Skill ZIP 上传下载、版本记录、搜索、统计、AI 助手和内置 Agent。页面使用紫色与浅粉色视觉风格。

## 公开数据

- 6176 条资源：5963 条预设、213 个非 Flow Skill。
- 2549 张封面，数据库和静态 JSON 都由脱敏快照生成。
- **Flow 平台的 75 个 Skill 全部排除**；两条含 Flow Skill 内容或明确标注 Flow 的 TV Skill 也已排除。其 ZIP、解压正文、版本、文件索引和分析结果都未进入这个仓库。
- 账号、密码哈希、会话、操作记录、Agent 对话、Cookie、模型 Key 与本机配置未收录。

资源详情保留来源字段；界面与应用实现作为个人项目展示，资源正文不宣称为作者原创。

## 运行完整应用

```powershell
python scripts/restore_public_data.py
python server/app.py
```

默认访问 <http://127.0.0.1:8000>。完整说明见 [server/README.md](server/README.md)。Agent 需要自行配置模型 API Key。公开站根目录的 `index.html` 是静态介绍与资源浏览页，不能替代完整应用。

## 静态页预览

在仓库根目录运行 `python -m http.server 8001`，访问 <http://127.0.0.1:8001>。静态页能浏览、筛选和复制公开资源。
