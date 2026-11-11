# xiaohongshu-cli 能力审计（获客助手）

审计日期：2026-10-20；仓库版本：0.6.4；许可证：Apache-2.0；Python：>=3.10。

## 可复用能力

| 能力 | 内部接口 | CLI | 结论 |
|---|---|---|---|
| 登录 | `cookies.get_cookies`、二维码登录 | `xhs login` | 可复用；凭证按本地账号目录保存 |
| 搜索 | `XhsClient.search_notes` | `xhs search --sort latest --page N` | 支持关键词、排序、页码；默认每页 20 |
| 详情 | `XhsClient.get_note_detail` | `xhs read` | 有 token 时优先 Feed API，失败后回退 HTML |
| 评论 | `XhsClient.get_comments` | `xhs comments` | 需要有效 `xsec_token`；异常不能解释为零评论 |
| 结构化输出 | 原始 API 字典 | `--json/--yaml` | 可用，但获客模块应调用内部接口，不解析终端文本 |

## 字段映射

搜索项位于 `data.items[]`，核心字段来自：

- ID：`item.id` 或 `note_card.note_id`
- 标题：`note_card.title` 或 `note_card.display_title`
- 正文：`note_card.desc`（搜索结果可能缺失）
- 作者：`note_card.user.nickname/user_id`
- 评论数：`note_card.interact_info.comment_count`
- 发布时间：`note_card.time`（可能缺失；展示用 `corner_tag_info` 不能作为精确时间）
- 访问令牌：`item.xsec_token`

任何缺失的评论数字段必须保留为 `None`。旧的展示 normalizer 会把详情中的缺失评论数显示为 `"0"`，获客模块不得使用该默认值。

## 已知限制

- 接口为逆向 Web API，字段和可用性可能变化，不能保证全量覆盖。
- 搜索“最新”由上游排序实现；仍需用详情时间过滤窗口。
- 评论数在搜索后可能变化，人工联系前必须重新检查。
- 验证码、限流、账号失效时必须停止或退避，不能把失败当成空结果。
- 多账号的 Cookie、token、搜索会话和获客数据库均应隔离。
- 原帖公开正文可用于最小化分析；不得记录无关敏感信息。
