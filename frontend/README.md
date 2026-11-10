# 深研 · Research workspace

Vue 3 + TypeScript + Vite 前端。暖白纸面、墨绿强调色，面向研究任务、报告阅读和证据回查。直接使用现有 FastAPI API，不更改研究流程。

## 本地启动

要求 Node.js 22.12+（本次开发使用 24.12.0）。两个终端分别启动：

```powershell
# 终端 1：项目 backend 目录
uv run uvicorn deep_researcher.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

```powershell
# 终端 2：项目 frontend 目录
npm.cmd ci
npm.cmd run dev
```

打开 http://127.0.0.1:5173 。开发服务器将 `/api` 转发至 `http://127.0.0.1:8000`。可以在启动 Vite 前通过 `RESEARCH_API_URL` 环境变量覆盖目标地址。前端不需要、也不应保存模型密钥。

## 验证

```powershell
npm.cmd run build
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

浏览器测试使用拦截的 API 响应，不调用模型或搜索服务，不写入后端研究数据。覆盖创建、进度轮询、失败与中断恢复、网络错误、历史分页、引用抽屉、Markdown 安全渲染、下载、刷新与移动布局。截图保存在忽略提交的 `test-results/` 中。

## 行为边界

- URL 使用 Hash 路由，详情页刷新后仍可根据完整 `run_id` 查询。
- 进度显示已提交的检查点，待执行节点不冒充精确实时执行信息。任务处理进度包含完成与跳过任务，不代表最终报告完成百分比。
- 查询可自动重试；创建和恢复不会自动重试。创建响应不确定时，必须先检查历史再手动提交。
- 页面隐藏时暂停轮询；切换研究取消旧请求。关闭网页不取消后台任务。
- 引用抽屉仅展示最终报告引用的证据，符合当前结果 API 的范围。
- 报告禁用原始 HTML，渲染后清理 HTML，再按已知证据编号生成引用按钮。代码片段中的编号不被替换。
- 暂不提供后端未实现的删除、取消、文件上传、RAG 功能。

## 生产构建

`npm.cmd run build` 生成 `dist/`。可以用 `npm.cmd run preview` 在本机预览。正式部署时需要静态托管 `dist/`，并由反向代理把 `/api` 转发给后端。Vite 开发服务不是生产服务器。当前后端按单用户、单进程运行，不应把无鉴权 API 直接公开到互联网。

## 目录

- `src/api.ts`：请求、取消、超时和统一错误。
- `src/composables/useResearch.ts`：详情轮询与生命周期。
- `src/App.vue`：布局、健康状态与历史分页。
- `src/views/`：创建与研究详情。
- `src/components/`：品牌图形、报告、证据抽屉。
- `src/style.css`、`src/styles/report.css`：工作台与文档阅读样式。
- `tests/workspace.spec.ts`：浏览器交互验证。
