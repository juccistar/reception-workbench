# 0.4.2 设置与表单交接

- `useWorkbench` 新增 `registerFormSaver(id, save)`，关闭保存事件依次等待已登记的脏表单，再保存客户补充和正文；未登记、失败、冲突或保存期间再次编辑会阻止关闭。`SettingsPage` 的处理设置、接待范围和界面布局，以及 `PoliciesPage` 已注册。飞书表单须由其负责线程以相同 ID 注册实际返回布尔值、核对编辑版本的保存函数。
- 导航当前仍通过 `canLeaveForms` 提示并阻止离开；未完成设计稿要求的应用内可交互确认。主线程需在 `App.tsx` 接线自有确认弹窗及保存/继续编辑协调，不能使用原生 `window.confirm`，也不能在有脏表单时直接卸载页面。
- 平台连接改为状态与操作行，连接详情按需展开。界面布局保存 `nav_preference` 和 `details_preference`，`changePreferences` 返回保存是否成功。宿主 `main/index.ts` 的默认 `details_width` 仍为 360；主线程应协调改为 334 并检查 `App.tsx` 对旧 360 的兼容、导航偏好及资料栏收起行为。
- 跨客户选择显式事项时保留 case_id 并固定该视图，不调用更新当前事项或重开事项的操作。
- 定向结果：`pages-042.test.tsx` 和 `policy-library.test.tsx` 10 项通过；`renderer-workbench.test.tsx` 中新增的关闭失败和跨客户旧事项两项通过；桌面 TypeScript 检查通过。整份 `renderer-workbench.test.tsx` 运行 124 项时有 24 项失败（本次新增两项之前），涉及现有界面断言等，不能计为通过；其中资料页异步目录返回异常已修正，但整份未重跑。未运行全量测试、GUI 或构建，未完成截图逐项设计验收。
