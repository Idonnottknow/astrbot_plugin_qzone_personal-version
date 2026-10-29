# astrbot_plugin_qzone_personal_version

![version](https://img.shields.io/badge/version-v3.2.0-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-4.x-orange)
![License](https://img.shields.io/badge/License-MIT-green)

_✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) QQ空间对接插件 · 个人版 ✨_

本仓库是 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 的个人分支（基于 v3.1.3），主要改动是**机器人自己发说说前先交给管理员审核**，其余功能与原版一致。

## ✨ 相比原版多了什么

- **发说说审核**：机器人自己决定发说说时（聊天中调用工具、定时自动发），不会直接发到空间，而是先落库成待审稿件，把卡片推给管理员；管理员「过稿」后才真正发布，「拒稿」则作废。
- **待审列表命令**：`待审` 查看当前排队等待审核的说说。
- **审核结果回发**：通过 / 拒绝后可以把结果和卡片发回机器人当初被触发的群聊。
- **过稿 / 拒稿按稿件类型分流**：表白墙投稿仍走原有刊头与投稿人通知，机器人待审说说直接发布或丢弃，不会互相串味。

除上述内容外，查看 / 点赞 / 评论说说、定时自动发说说、表白墙投稿与审核等功能均与原版相同。

## 📦 安装

在 AstrBot 面板的插件市场右上角选择「从仓库安装」（或「从链接安装」），填入本仓库地址：

```text
https://github.com/Idonnottknow/astrbot_plugin_qzone_personal-version
```

也可以克隆到插件目录后重启 AstrBot：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/Idonnottknow/astrbot_plugin_qzone_personal-version.git
```

> 插件名是 `astrbot_plugin_qzone_personal_version`，与原版的 `astrbot_plugin_qzone` 是两个不同插件，**不要同时安装**，否则命令和 LLM 工具会重复注册。

## 🔄 更新

本插件已把更新源绑定到本仓库，「更新」只会从本仓库拉取，不会去动插件市场里的原版。

GitHub 在国内直连偶尔会超时，更新时如果报下载失败，在更新弹窗的「下载加速 / 代理」里填一个加速前缀再试：

```text
https://ghfast.top
```

## ⌨️ 配置

请前往插件配置面板进行配置。说说和系统消息统一使用内置卡片渲染器，渲染失败时自动回退为纯文本。

### 发说说审核（本分支新增）

配置项位于面板的「发说说审核」分组：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `review.enable` | `true` | 总开关，关闭后所有说说仍直接发布 |
| `review.apply_llm` | `true` | 机器人在聊天中调用工具发说说时审核 |
| `review.apply_auto` | `true` | 定时自动发说说时审核 |
| `review.apply_command` | `false` | 管理员执行「发说说」命令时也先过审 |
| `review.notify_origin` | `true` | 审核通过 / 拒绝后，把结果回发到机器人当初被触发的群聊 |

审核消息的收件人：配置了「管理群」就发到管理群，否则私聊 AstrBot 管理员账号。**如果管理群和管理员都没配置，插件会拒绝发布并提示，不会静默绕过审核。**

完整流程：

```text
机器人想发说说 → 落库为待审稿件（pending_bot）→ 卡片 + 提示发给管理员
                → 管理员「过稿 N」→ 真正发布到 QQ 空间
                → 管理员「拒稿 N 原因」→ 作废，不发布
```

审核消息发出时，QQ 空间里**还没有**这条说说；此时 AI 拿到的工具返回值会明确告知「已提交审核」，不会声称已经发布。

### 定时调度说明

- `publish_cron` / `comment_cron` 表示任务的**基准时间**（Cron 表达式：分 时 日 月 周）。
- `publish_offset` / `comment_offset` 表示围绕基准时间的随机前后浮动范围（单位秒，`±N`）。
- 例如 `publish_cron = 30 23 * * *` 且 `publish_offset = 600`，表示每天 23:30 前后 10 分钟内随机执行一次自动发说说。
- 偏移设为 `0` 时严格按 Cron 时间触发。

## 🐔 使用说明

### 基础说明

- 默认查看的是「好友动态流」
- `@某人` / `@QQ号`：表示查看该用户的 QQ 空间
- 序号从 `0` 开始：`0` = 最新一条，`-1` = 最后一条
- 支持范围语法：`2~5`
- 机器人在评论 / 回复时会自动排除自己的评论

### 命令一览表

| 命令 | 别名 | 权限 | 参数 | 功能说明 |
|---|---|---|---|---|
| 查看访客 | - | ADMIN | - | 查看 QQ 空间最近访客列表 |
| 看说说 | 查看说说 | ALL | `[@用户] [序号/范围]` | 查看说说（自动拉取完整详情） |
| 评说说 | 评论说说、读说说 | ALL | `[@用户] [序号/范围]` | 给说说评论（可配置自动点赞） |
| 赞说说 | - | ALL | `[@用户] [序号/范围]` | 给说说点赞 |
| 发说说 | - | ADMIN | `<文本> [图片]` | 发布一条说说（开启对应审核时先过审） |
| 写说说 | 写稿 | ADMIN | `<主题> [图片]` | AI 生成草稿，保存为待审稿件 |
| 删说说 | - | ADMIN | `<序号>` | 删除自己发布的说说 |
| 回评 | 回复评论 | ALL | `<稿件ID> [评论序号]` | 回复评论（默认回复最后一条非自己评论） |
| 投稿 | - | ALL | `<文本> [图片]` | 向表白墙投稿 |
| 匿名投稿 | - | ALL | `<文本> [图片]` | 匿名投稿到表白墙 |
| 看稿 | 查看稿件 | ADMIN | `[稿件ID]` | 查看稿件（默认最新） |
| 待审 | 待审核、审核列表 | ADMIN | - | 查看机器人待审核的说说 |
| 过稿 | 通过稿件、通过投稿 | ADMIN | `<稿件ID>` | 审核并发布（表白墙投稿 / 机器人待审说说） |
| 拒稿 | 拒绝稿件、拒绝投稿 | ADMIN | `<稿件ID> [原因]` | 拒绝稿件 |
| 撤稿 | - | ALL | `<稿件ID>` | 撤回自己投稿的稿件 |

### 范围参数示例

```text
看说说
看说说 2
看说说 1~3
看说说 @某人
看说说 @某人 0
```

## 💡 TODO

- [x] 发说说
- [x] 校园表白墙功能：投稿、审核投稿
- [x] 点赞说说（接口显示成功，但实测点赞无效）
- [x] 评论说说
- [x] 定时自动发说说、日记
- [x] 定时自动评论、点赞好友的说说
- [x] LLM 发说说
- [x] **机器人发说说前管理员审核**
- [ ] LLM 配图
- [ ] 自动上网冲浪写说说

## 📌 说明

- 本仓库是个人分支，功能建议与原版问题请优先反馈给 [原作者仓库](https://github.com/Zhalslar/astrbot_plugin_qzone)。
- 数据（稿件、待审记录）保存在 `data/plugin_data/astrbot_plugin_qzone/`，更新插件不会清空。
- 配置保存在 `data/config/astrbot_plugin_qzone_config.json`，由插件目录名决定，因此改名 / 换仓库后配置仍然沿用。

## 🤝 鸣谢

- 原项目：[Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)（MIT）
- 部分代码参考了 [CampuxBot](https://github.com/idoknow/CampuxBot)，由作者之一的 Soulter 推荐
- [QQ 空间爬虫之爬取说说](https://kylingit.com/blog/qq-空间爬取说说/) 提供了爬取思路
- [QzoneExporter](https://github.com/wwwpf/QzoneExporter)
- QQ 空间网页显示本地数据时使用的样式与布局均来自于 [QQ空间](https://qzone.qq.com/)
