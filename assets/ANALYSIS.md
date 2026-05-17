# dev branch 完成内容分析

> 基于 `github/dev` 分支（42f1f66），对比 `main` 分支，分析dev分支完成的工作内容。
> 本地dev分支已被删除，但远程 `github/dev` 和 `gitea/dev` 仍可用。
> 每个工作区域对应的diff已导出到 `dev_diffs/` 目录，可作为实现参考。

## 整体规模

- 106 commits 在 main 分叉点之上（包含与 main 共享的早期历史）
- 103 个文件变更，+10379/-2809 行
- 主要工作覆盖：连接器系统、前端重构、右键菜单重写、批量操作、HTTP请求支持、UI按钮重构
- 9个按工作区域组织的diff文件（共9416行），位于 `dev_diffs/`

## 已完成的工作

### 1. Connector（连接器）系统（核心架构变更）
> diff参考: [`dev_diffs/01_connector_system.diff`](./dev_diffs/01_connector_system.diff)（核心逻辑，346行）
> diff参考: [`dev_diffs/02_connector_frontend.diff`](./dev_diffs/02_connector_frontend.diff)（前端页面，418行）

这是dev分支最早启动的大型重构，将Webshell管理统一到connector体系：

- **数据模型**：新增 `SessionConnectorInfo` 及对应的数据库表 `SessionConnectorModel`
- **持久化层**：新增 connector CRUD 数据库操作函数
- **API层**：新增 `ether_ghost/api/connector.py`，提供连接器的增删改查API
- **前端**：
  - `ConnectorMain.vue` - 连接器列表页，展示所有连接器及其状态
  - `ConnectorEditorMain.vue` - 连接器编辑页，支持新增/编辑连接器配置（基于 `GroupedForm.vue`）
  - 空连接器时展示引导提示和添加按钮
  - 自动启动连接器功能
- **实现**：
  - `ReverseShellConnector` - 反弹Shell连接器，持久化监听端口
  - 连接器的启动/停止/自动重连
  - 连接器注册机制（`@register_connector` 装饰器）
- **删除旧代码**：
  - 移除旧的 `Remove test connector button`
  - 删除 `psudo_shell.py`

### 2. 前端构建流程重构
> diff参考: [`dev_diffs/09_build_system.diff`](./dev_diffs/09_build_system.diff)（380行）

- **脚本重构**：从根目录 `build.sh` 迁移到 `script/build.sh` + `script/build_frontend.py`
- **Flake更新**：更新 `flake.nix` 和 `flake.lock`
- **字体更换**：从 FiraCode Nerd Font 替换为 MapleMono NF（更轻量）
- **前端依赖更新**：`package.json` 和 `package-lock.json` 更新

### 3. 右键菜单系统重构（ClickMenuDualLayer）
> diff参考: [`dev_diffs/03_right_click_menu.diff`](./dev_diffs/03_right_click_menu.diff)（876行）

- **新组件**：`ClickMenuDualLayer.vue` - 双层右键菜单，支持一级菜单和二级菜单
- **替换旧系统**：删除了旧的 `ClickMenu.vue` 和 ClickMenuManager，改用 `ClickMenuManagerDualLayer`
- **功能**：
  - 右键点击session弹出功能列表
  - 点击 session-icon-others 按钮也弹出菜单
  - 二级菜单：对接蚁剑等选项展开子菜单（打开/在新标签页打开）
  - 所有选项支持自定义图标、文本、颜色和点击行为
  - 新增多选模式入口（"多选webshell"选项）
  - 新增批量操作右键菜单（批量测试、批量执行命令等）

### 4. 批量操作功能
> diff参考: [`dev_diffs/04_batch_operations.diff`](./dev_diffs/04_batch_operations.diff)（835行）

#### 4a. 多选模式
- 支持多选Webshell
- 右键菜单添加"多选Webshell"选项
- 多选模式自动退出逻辑
- 禁止文字选中以优化点击体验

#### 4b. 批量测试Webshell
- 批量选择后一键测试所有选中的Webshell
- 并发测试（非串行） - 使用 `Promise.allSettled` 并发执行
- 状态小圆点显示测试结果（红/绿/黄闪）
- 独立 `batchOperations.js` 工具函数（130行），包含 `initBatchOperationStatus`、`cleanupBatchOperation`、`batchPrintToConsole`、`batchTestWebshell`

#### 4c. 批量执行命令（BatchCommandPage）
- **前端页面**：`BatchCommandPage.vue`（692行）
- 命令输入框 + 执行按钮 + 导出按钮
- 滚动展示每行执行结果
- 状态小圆点：黄闪（执行中）、红（失败）、绿（成功）
- **功能对接**：
  - 并发调用API执行命令
  - 导出结果JSON
  - 通过Vue Router参数传递选中的session ID

### 5. HTTP请求发送功能
> diff参考: [`dev_diffs/05_http_request_and_jsp.diff`](./dev_diffs/05_http_request_and_jsp.diff)（1313行，含JSP支持）

- **接口扩展**：`SessionInterface` 新增 `send_http` 方法
  - 输入：url, method, header, param, data
  - 输出：status code, header, body
- **实现**：
  - PHP：使用 curl 模块（非 curl 命令）
  - Linux Shell：使用 curl 命令
  - JSP：使用 Java 内置 HTTP 库
- **API暴露**：新增发送HTTP请求的API端点
- **JSP支持**：完整的Behinder JSP webshell实现（文件操作、命令执行、HTTP请求等）

### 6. 后端架构重构
> diff参考: [`dev_diffs/06_backend_refactor.diff`](./dev_diffs/06_backend_refactor.diff)（2279行）

- **API模块化**：从庞大的 `main.py`（663行）拆分为独立模块：
  - `ether_ghost/api/` 目录，包含 `base.py`, `connector.py`, `forward_proxy.py`, `session.py`, `sessiontype.py`, `settings.py`, `utils.py`
- **session_connector重构**：
  - 删除注释，调整import
  - 移除元编程（`register_direct_session_class`）
  - 改用装饰器在各个webshell实现中手动注册
- **新增模块**：
  - `ether_ghost/file_transfer_status.py` - 文件传输状态管理
  - `ether_ghost/sessions/reverse_shell.py` - 反弹Shell session实现
  - `ether_ghost/session_manager.py` - 缓存优化（300秒TTL）

### 7. 通用前端组件库
> diff参考: [`dev_diffs/07_frontend_components.diff`](./dev_diffs/07_frontend_components.diff)（414行）

在dev分支上创建了多个可复用组件：

| 组件 | 用途 |
|---|---|
| `ClickMenuDualLayer.vue` | 双层右键菜单 |
| `GroupedForm.vue` | 分组表单组件 |
| `LoadingButton.vue` | 带加载状态的按钮 |
| `StatusIndicator.vue` | 状态指示器 |
| `WebshellTypeDot.vue` | WebShell类型圆点 |
| `iconPause.vue` | 暂停图标 |
| `iconUsb.vue` | USB图标 |

### 8. UI按钮重构（进行中，未完成）
> diff参考: [`dev_diffs/08_ui_button_refactor.diff`](./dev_diffs/08_ui_button_refactor.diff)（2555行，涉及FileBrowserMain/Settings/WebshellEditorMain等页面的修改）

**TASK.md 中最后一项未完成的任务**：
- 需要重构多个页面的按钮样式
- 期望：统一使用 `LoadingButton.vue` 组件
- 涉及页面：文件管理、机器信息、代理界面、连接器界面、设置界面、PHP代码执行界面、反弹shell界面
- 要求使用视觉大模型验证

## 待完成的工作

1. **UI按钮重构**（TASK.md 中唯一未勾选的任务）
   - 文件管理界面 - 保存按钮
   - 机器信息界面 - 打开/下载phpinfo按钮和刷新按钮
   - 代理界面 - 添加代理按钮
   - 连接器界面 - 运行连接器按钮和设置按钮
   - 设置界面 - 测试代理按钮
   - PHP代码执行界面 - 执行/使用eval执行/使用include执行
   - 反弹shell界面 - 连接按钮

## 分支状态

- 远程分支 `github/dev` 和 `gitea/dev` 均完好可用
- 最新commit: `42f1f66` (Update TASK, 2026-01-27)
- 本地 `dev` 分支已被删除，可通过 `git branch dev github/dev` 恢复
- 所有工作成果的diff已导出到 `dev_diffs/` 目录，包含9个文件共9416行
