# astrbot_plugin_qzone_personal_version

![version](https://img.shields.io/badge/version-v3.2.0-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-4.x-orange)
![License](https://img.shields.io/badge/License-MIT-green)

_✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) QQ空间对接插件 · 个人版 ✨_

本仓库是 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 的个人分支（基于 v3.1.3）。主要改动是**机器人自己发说说前先交给管理员审核**，此外还累积了一批评论、回复、昵称显示相关的修复（见下文「已包含的历史修复」）。

## ✨ 相比原版多了什么

- **发说说审核**：机器人自己决定发说说时（聊天中调用工具、定时自动发），不会直接发到空间，而是先落库成待审稿件，把卡片推给管理员；管理员「过稿」后才真正发布，「拒稿」则作废。
- **待审列表命令**：`待审` 查看当前排队等待审核的说说。
- **审核结果回发**：通过 / 拒绝后可以把结果和卡片发回机器人当初被触发的群聊。
- **过稿 / 拒稿按稿件类型分流**：表白墙投稿仍走原有刊头与投稿人通知，机器人待审说说直接发布或丢弃，不会互相串味。

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

## 🧰 已包含的历史修复

以下改动早于「发说说审核」，都已包含在当前代码里，按当时的症状整理。

### 评论与回复

- **评论不再「假成功」**：`comment_posts()` 会检查接口返回值，失败直接抛错并在命令里显示真实原因，不再把失败记成「已评论」写进库。
- **同一条说说不会反复评论**：判断前一律用详情接口刷新评论树（`with_detail=True` 时跳过重复请求）；`db.save()` 在新数据缺评论 / 昵称时保留旧值，避免把「已评论」记录洗掉；并对同一 `uin:tid` 加并发锁，防止两条消息同时触发时各评一条。
- **回复不再退化成普通评论**：`reply()` 优先走 PC 代理（`topicId` 不带 `__1`，带 `commentId` / `replyUin` / `commentUin`），失败回退 h5 并补齐楼层参数（`t1_uin` / `t1_tid` / `t2_uin` / `t2_tid`）；响应无法解析时不回退，避免重复回复；同时把 `ret` / `code` 归一化，不再误判失败。
- **回复不会挂到错误的人或评论**：`reply_comment()` 先刷新评论树拿服务端真实 `tid`，再按「作者 + 去掉空白的正文」回查目标评论（同作者多条同文按出现次序匹配）；插件页回复用 `commentid` + `comment_uin` 一起定位，楼中楼回复自动挂到主评论下并 @ 对方。
- **插件页操作会写回数据库**：插件页里评论 / 回复后会落库，`回评` 和去重逻辑能看到刚发的评论（原来只写内存）。
- **点赞不再偶发 AttributeError**：`core/qzone/client.py` 里对 `data` 先做 `isinstance(dict)` 判断，点赞成功的空字符串响应不会再崩。
- **详情接口仍走 h5**：没有跟随上游 PR #66 把 `DETAIL_URL` 改成 `user.qzone.qq.com`，避免影响插件页的详情 / 评论展示；要完全对齐上游只需改 `core/qzone/api.py` 里 `DETAIL_URL` 一行。

### 昵称与显示

- **作者名不再被评论者覆盖**：`parse_recent_feeds()` 里作者昵称和评论昵称曾共用同一个变量，只要有条评论，`Post.name` 就会变成最后一条评论者（或空串）；现在拆成 `author_nick`。
- **评论昵称取不到**：HTML 评论在 `data-nick` 缺失时回退到正文里的昵称链接；`Comment.from_raw()` 不再只认 `name`，兼容 `nickname` / `nick` / `nickName`，并对 `uin` / `tid` / `create_time` 做安全转换，避免一条脏数据让整批说说解析失败。
- **正文不再出现「昵称：昵称：」**：原来用半角冒号 `split`，切不掉 QQ 空间的全角「昵称：」，正文里的半角冒号还会被截断；现在只在开头确实等于已知昵称时才剥离，全角 / 半角都处理，不会误伤「12:30 吃饭」。
- **插件页不再显示 `@{uin:...}` 原始标记**：`_clean_page_text()` 把 `@{uin:123,nick:某某,who:1,auto:1}` 还原成 `@某某`，并去掉 `[em]` 表情标记；只匹配带 `uin:` 的标记，不会误伤普通 `{xxx}`。

### LLM 工具

- **可以传评论原文，并按 tid 精确评论**：新增 `llm_comment_post(content, user_id, tid, pos, like)`；`llm_view_feed` 也支持 `tid` 与 `content`，返回值里带 `uin=... tid=...`，模型下次可以直接引用，不会因为序号漂移评到别的说说上。
- **评论里的 @ 会变成真提及**：`resolve_mentions()` 把 `@QQ号` 转成 `@{uin:...,nick:...,auto:1}`，`@昵称` 在候选里精确匹配后转换（长昵称优先，避免「张三丰」被「张三」截断）；候选来自当前聊天对象、说说作者和这条说说下的所有评论者；模型不知道对方昵称时可以写 `@sender` 指代当前聊天对象。认不出 uin 的 @ 保持纯文本，普通邮箱也不会被误伤。
- **支持机器人自己的空间**：`user_id` 填 `self` / `bot` / `me` / `自己` / `我` / `本人` / `本机` 即机器人自己的 QQ；`@123456` 会去掉 `@`；不填仍默认当前聊天用户。
- **QQ 空间操作仅限管理员**：三个 LLM 工具都会校验 `event.is_admin()` 或插件 `admins_id`，非管理员直接返回「无权限：QQ空间操作仅限管理员使用。」；`看说说` / `评说说` / `赞说说` / `回评` 等命令也补了 ADMIN 权限装饰器。

### 已知限制

- 插件页前端仍是平铺渲染所有评论，楼中楼不缩进（后端已下发 `parent_id`，不缩进但不影响功能）。
- 旧数据库里的错误昵称 / 评论不会自动修复，重新拉一次详情就会被覆盖。
- 动态流作者名依赖接口返回的 `nickname`，接口本身没给时只能回退成 QQ 号（插件页显示「QQ空间用户」）。
- `prob_read_feed`（随机读说说）是**消息被动触发**，不走指令 / 工具权限：其他用户发消息仍可能按 `trigger.read_prob` 概率触发评论。不想被触发就把 `read_prob` 设为 `0`，或把用户加进 `source.ignore_users`。
- `投稿` / `匿名投稿` 保持对所有人开放：它们只写本地待审稿件，不会直接操作 QQ 空间；发布仍需管理员 `过稿`。
