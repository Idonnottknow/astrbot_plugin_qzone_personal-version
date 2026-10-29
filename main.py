import base64
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.campus_wall import CampusWall
from .core.config import PluginConfig
from .core.db import PostDB
from .core.llm_action import LLMAction
from .core.model import PENDING_BOT_STATUS, Comment, Post, remove_em_tags
from .core.qzone import QzoneAPI, QzoneParser, QzoneSession
from .core.review import PublishReview
from .core.scheduler import AutoComment, AutoPublish
from .core.sender import Sender
from .core.service import PostService
from .core.utils import get_ats, get_image_urls, parse_range

try:
    from quart import jsonify as _quart_jsonify
    from quart import request as _quart_request
except Exception:
    _quart_jsonify = None
    _quart_request = None


# user_id 里表示“机器人自己”的写法
_SELF_USER_ALIASES = {"self", "bot", "me", "myself", "自己", "我", "本人", "本机"}


class QzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        # 配置
        self.cfg = PluginConfig(config, context)
        # 会话
        self.session = QzoneSession(self.cfg)
        # QQ空间
        self.qzone = QzoneAPI(self.session, self.cfg)
        # 数据库
        self.db = PostDB(self.cfg)
        # LLM模块
        self.llm = LLMAction(self.cfg)
        # 消息发送器
        self.sender = Sender(self.cfg)
        # 操作服务
        self.service = PostService(self.qzone, self.session, self.db, self.llm)
        # 发说说审核（机器人自主发说说时先交给管理员过稿）
        self.review = PublishReview(
            self.cfg, self.service, self.db, self.session, self.sender
        )
        # 表白墙
        self.campus_wall = CampusWall(self.cfg, self.service, self.db, self.sender)
        # 自动评论模块
        self.auto_comment: AutoComment | None = None
        # 自动发说说模块
        self.auto_publish: AutoPublish | None = None
        self._page_post_refs: dict[str, Post] = {}
        self._register_page_web_apis()

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()

        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)

        if not self.auto_publish and self.cfg.trigger.publish_cron:
            self.auto_publish = AutoPublish(
                self.cfg, self.service, self.sender, self.review
            )

    def _register_page_web_apis(self) -> None:
        routes = (
            ("page/status", self.page_status, ["GET"], "Qzone dashboard status"),
            ("page/feed", self.page_feed, ["GET"], "Qzone dashboard feed"),
            ("page/detail", self.page_detail, ["GET"], "Qzone dashboard detail"),
            ("page/publish", self.page_publish, ["POST"], "Qzone dashboard publish"),
            ("page/like", self.page_like, ["POST"], "Qzone dashboard like"),
            ("page/comment", self.page_comment, ["POST"], "Qzone dashboard comment"),
            ("page/reply", self.page_reply, ["POST"], "Qzone dashboard reply"),
            ("page/delete", self.page_delete, ["POST"], "Qzone dashboard delete"),
            (
                "page/upload-media",
                self.page_upload_media,
                ["POST"],
                "Qzone dashboard upload media",
            ),
        )
        for endpoint, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/astrbot_plugin_qzone/{endpoint}",
                handler,
                methods,
                desc,
            )

    async def _page_response(self, payload: dict, status: int = 200):
        if _quart_jsonify is None:
            return payload
        response = _quart_jsonify(payload)
        response.status_code = status
        return response

    async def _page_json(self, callback):
        try:
            payload = await callback()
            status = 200
        except Exception as exc:
            logger.exception("qzone page api failed: %s", exc)
            message = str(exc) or "请求失败"
            payload = {
                "ok": False,
                # AstrBot 的插件页桥接只识别字符串类型的 message/error，
                # 补一个顶层 message，避免前端把真实异常统一显示成
                # “Request failed with status code 400”。
                "message": message,
                "error": {"message": message},
            }
            status = 400
        return await self._page_response(payload, status)

    async def _page_query_params(self) -> dict:
        if _quart_request is None:
            return {}
        args = getattr(_quart_request, "args", {}) or {}
        try:
            return {str(key): value for key, value in args.items()}
        except Exception:
            return dict(args)

    async def _page_json_body(self) -> dict:
        if _quart_request is None:
            return {}
        try:
            data = await _quart_request.get_json(silent=True)
        except TypeError:
            data = await _quart_request.get_json()
        return data if isinstance(data, dict) else {}

    async def _build_page_status(self) -> dict:
        self._capture_page_client()
        try:
            uin = await self.session.get_uin()
            nickname = await self.session.get_nickname()
            bound = True
        except Exception:
            uin = 0
            nickname = ""
            bound = False
        return {
            "ok": True,
            "data": {
                "daemon": {"state": "ready" if bound else "offline"},
                "login": {
                    "bound": bound,
                    "uin": uin,
                    "nickname": nickname,
                    "avatar": "",
                },
                "limits": {
                    "feed": 10,
                    "images": 9,
                },
            },
        }

    def _remember_page_post(self, post: Post) -> str:
        post_id = f"{post.uin}:{post.tid}"
        self._page_post_refs[post_id] = post
        return post_id

    def _capture_page_client(self):
        if self.cfg.client is not None:
            return self.cfg.client
        try:
            platform = self.context.get_platform("aiocqhttp")
        except Exception:
            platform = None
        if platform is None:
            try:
                for candidate in self.context.platform_manager.platform_insts:
                    meta = candidate.meta()
                    if getattr(meta, "name", "") == "aiocqhttp":
                        platform = candidate
                        break
            except Exception:
                platform = None
        bot = getattr(platform, "bot", None) if platform is not None else None
        if bot is not None:
            self.cfg.client = bot
        return self.cfg.client

    _SELF_ALIASES = _SELF_USER_ALIASES

    def _is_admin(self, event: AiocqhttpMessageEvent) -> bool:
        """当前消息发送者是否是 AstrBot 管理员。

        AstrBot 的 event.is_admin() 依赖全局 admins_id；插件配置里的
        admins_id 也是同一来源，这里做个兜底，避免角色字段没同步时
        管理员自己也被挡住。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender = str(event.get_sender_id())
        try:
            admin_ids = {str(x) for x in (self.cfg.admins_id or [])}
        except Exception:
            admin_ids = set()
        return sender in admin_ids

    async def _resolve_target_uin(
        self, user_id: str | None, event: AiocqhttpMessageEvent
    ) -> str:
        """把工具里的 user_id 规范成 QQ 号。

        - self / bot / 自己 / 我 等 → 机器人自己的 QQ（用于评论自己发的说说）
        - 不填 → 当前聊天用户
        - @123456 → 去掉 @ 后按 QQ 号
        """
        value = str(user_id or "").strip()
        if value.startswith("@"):
            value = value[1:].strip()
        if value.lower() in _SELF_USER_ALIASES:
            uin = await self.session.get_uin()
            if not uin:
                raise RuntimeError("还没拿到机器人自己的QQ号")
            return str(uin)
        if not value:
            return str(event.get_sender_id())
        if not value.isdigit():
            raise ValueError(f"无效的QQ号：{value}")
        return value

    @staticmethod
    def _clean_page_text(value: str | None) -> str:
        """插件页展示用的纯文本。

        去掉 [em] 表情标记，并把 QQ 空间的提及标记
        ``@{uin:123,nick:某某,who:1,auto:1}`` 还原成 ``@某某``。
        原实现直接把 content 原样给前端，插件页里就会看到
        “@{uin:2786910486,nick:L,auto:1} 回复内容”这种原始标记。

        这里只替换带 uin 的提及标记，不会误伤正文里普通的 {xxx}。
        """
        if not value:
            return ""
        text = str(value)

        def replace_mention(match: re.Match[str]) -> str:
            body = match.group(0).strip("@{} ")
            nick = ""
            for pair in body.split(","):
                if ":" not in pair:
                    continue
                key, val = pair.split(":", 1)
                if key.strip() == "nick":
                    nick = val.strip()
                    break
            return f"@{nick}" if nick else ""

        text = re.sub(r"@?\{uin:\s*\d+\s*[^{}]*\}", replace_mention, text)
        return remove_em_tags(text).strip()

    def _page_post_payload(
        self,
        post: Post,
        *,
        include_comments: bool = False,
        self_uin: int = 0,
    ) -> dict:
        images = list(dict.fromkeys([*(post.images or []), *(post.videos or [])]))
        payload = {
            "id": self._remember_page_post(post),
            "author": {
                "uin": post.uin,
                "nickname": self._clean_page_text(post.name) or str(post.uin),
                "avatar": post.avatar_url or "",
            },
            "content": self._clean_page_text(post.text)
            or self._clean_page_text(post.rt_con),
            "created_at": int(post.create_time or 0),
            "stats": {
                "likes": 0,
                "comments": len(post.comments or []),
            },
            "liked": False,
            "images": images,
            "can_delete": bool(self_uin and int(post.uin) == int(self_uin)),
        }
        if include_comments:
            payload["comments"] = [
                {
                    "id": str(comment.tid or index),
                    "author": {
                        "uin": comment.uin,
                        "nickname": self._clean_page_text(comment.nickname)
                        or str(comment.uin),
                    },
                    "content": self._clean_page_text(comment.content),
                    "created_at": int(comment.create_time or 0),
                    "can_reply": bool(comment.tid and comment.uin),
                    "parent_id": str(comment.parent_tid)
                    if comment.parent_tid
                    else None,
                }
                for index, comment in enumerate(post.comments or [], start=1)
            ]
        return payload

    def _require_page_post(self, post_id: str) -> Post:
        post = self._page_post_refs.get(str(post_id or "").strip())
        if not post:
            raise RuntimeError("说说引用已失效，请刷新页面后重试")
        return post

    async def page_status(self):
        return await self._page_json(self._build_page_status)

    async def page_feed(self):
        async def handler():
            self._capture_page_client()
            params = await self._page_query_params()
            scope = str(params.get("scope") or "friends")
            self_uin = await self.session.get_uin()
            target_id = None
            no_self = False
            if scope == "self":
                target_id = str(self_uin)
            elif scope == "profile":
                target_id = str(params.get("hostuin") or "").strip() or None
            else:
                no_self = False
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=0,
                num=min(max(int(params.get("limit") or 10), 1), 10),
                with_detail=False,
                no_self=no_self,
            )
            return {
                "ok": True,
                "data": {
                    "items": [
                        self._page_post_payload(post, self_uin=self_uin)
                        for post in posts
                    ],
                    "cursor": "",
                    "has_more": False,
                },
            }

        return await self._page_json(handler)

    async def page_detail(self):
        async def handler():
            self._capture_page_client()
            params = await self._page_query_params()
            post = self._require_page_post(params.get("id", ""))
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                raise RuntimeError(resp.message or "获取详情失败")
            parsed = QzoneParser.parse_feeds([resp.data])
            matched = parsed[0] if parsed else post
            self._remember_page_post(matched)
            return {
                "ok": True,
                "data": {
                    "post": self._page_post_payload(
                        matched,
                        include_comments=True,
                        self_uin=await self.session.get_uin(),
                    )
                },
            }

        return await self._page_json(handler)

    async def page_publish(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            media = body.get("media") or []
            images = []
            for item in media:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source") or "")
                if source.startswith("base64://"):
                    images.append(base64.b64decode(source.removeprefix("base64://")))
            post = await self.service.publish_post(
                text=str(body.get("content") or ""),
                images=images,
            )
            return {
                "ok": True,
                "data": {"post": self._page_post_payload(post)},
                "message": "说说已发布。",
            }

        return await self._page_json(handler)

    async def page_like(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            resp = await self.qzone.like(post)
            if not resp.ok:
                raise RuntimeError(resp.message or "点赞失败")
            return {"ok": True, "data": {"liked": True, "verified": True}}

        return await self._page_json(handler)

    async def page_comment(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            content = str(body.get("content") or "").strip()
            if not content:
                raise RuntimeError("评论内容不能为空")
            # 插件页评论里的 @昵称 / @QQ号 也转成真提及
            content = self.service.resolve_post_mentions(post, content)
            resp = await self.qzone.comment(post, content)
            if not resp.ok:
                raise RuntimeError(resp.message or "评论失败")

            # 不同代理把新评论 ID 放在不同层级（顶层 tid 或 data.tid）
            inner = resp.data.get("data")
            inner = inner if isinstance(inner, dict) else {}
            server_id = str(inner.get("tid") or resp.data.get("tid") or "")
            author_uin = await self.session.get_uin()
            author_nick = await self.session.get_nickname()
            created_at = int(time.time())

            new_comment = Comment(
                uin=author_uin,
                nickname=author_nick,
                content=content,
                create_time=created_at,
                tid=int(server_id) if server_id.isdigit() else 0,
                parent_tid=None,
            )
            post.comments.append(new_comment)
            # 原来只写内存不写库，导致“回评/去重”看不到插件页刚发的评论
            await self.db.save(post)

            comment = {
                "id": server_id or f"local-{len(post.comments)}",
                "content": self._clean_page_text(content),
                "author": {
                    "uin": author_uin,
                    "nickname": author_nick,
                    "avatar": "",
                },
                "created_at": created_at,
                "can_reply": bool(new_comment.tid),
                "parent_id": None,
            }
            return {"ok": True, "data": {"comment": comment}, "message": "评论已发送。"}

        return await self._page_json(handler)

    async def page_reply(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            content = str(body.get("content") or "").strip()
            comment_id = str(body.get("commentid") or "")
            comment_uin = str(
                body.get("comment_uin") or body.get("commentuin") or ""
            )
            if not content:
                raise RuntimeError("回复内容不能为空")

            # 插件页回复里的 @昵称 / @QQ号 也转成真提及
            content = self.service.resolve_post_mentions(post, content)

            # 楼中楼的 tid 是主评论内的楼层号（都从 1 开始），单靠 tid 会匹配到
            # 第一条同号评论；前端会带 comment_uin，用它一起定位。
            def _matches(item: Comment) -> bool:
                if str(item.tid) != comment_id:
                    return False
                return not comment_uin or str(item.uin) == comment_uin

            target = next((item for item in post.comments if _matches(item)), None)
            if target is None:
                raise RuntimeError("未找到要回复的评论")

            # 楼中楼回复：QQ 空间只有两级评论，回复楼中楼实际是在其主评论下
            # 再发一条 @对方的楼中楼，commentId 必须用主评论的 tid。
            reply_target = target
            reply_uin = None
            reply_nick = None
            target_comment_uin = None
            if target.parent_tid:
                main = next(
                    (
                        item
                        for item in post.comments
                        if not item.parent_tid
                        and str(item.tid) == str(target.parent_tid)
                    ),
                    None,
                )
                if main is None:
                    raise RuntimeError("未找到该楼中楼所属的主评论")
                reply_target = Comment(
                    uin=target.uin,
                    nickname=target.nickname,
                    content=target.content,
                    create_time=target.create_time,
                    tid=main.tid,
                    parent_tid=None,
                )
                reply_uin = target.uin
                reply_nick = target.nickname
                target_comment_uin = main.uin

            resp = await self.qzone.reply(
                post,
                reply_target,
                content,
                reply_uin=reply_uin,
                reply_nick=reply_nick,
                comment_uin=target_comment_uin,
            )
            if not resp.ok:
                raise RuntimeError(resp.message or "回复失败")

            # 不同代理把新评论 ID 放在不同层级（顶层 tid 或 data.tid），
            # 取不到时留空，后续详情刷新会补上。
            inner = resp.data.get("data")
            inner = inner if isinstance(inner, dict) else {}
            reply_id = str(inner.get("tid") or resp.data.get("tid") or "")
            author_uin = await self.session.get_uin()
            author_nick = await self.session.get_nickname()
            created_at = int(time.time())

            # 服务端是两级结构：回复楼中楼时新评论挂在主评论下（reply_target
            # 已被换成主评论），缓存里也要保持一致，否则下次按 parent_tid
            # 找不到所属主评论。
            cached_parent_tid = reply_target.tid if target.parent_tid else None
            post.comments.append(
                Comment(
                    uin=author_uin,
                    nickname=author_nick,
                    content=content,
                    create_time=created_at,
                    tid=int(reply_id) if reply_id.isdigit() else 0,
                    parent_tid=cached_parent_tid,
                )
            )
            await self.db.save(post)

            reply = {
                "id": reply_id or f"local-reply-{len(post.comments)}",
                "content": self._clean_page_text(content),
                "author": {
                    "uin": author_uin,
                    "nickname": author_nick,
                    "avatar": "",
                },
                "created_at": created_at,
                "can_reply": False,
                "parent_id": str(cached_parent_tid)
                if cached_parent_tid
                else str(target.tid or ""),
            }
            return {"ok": True, "data": {"reply": reply}, "message": "回复已发送。"}

        return await self._page_json(handler)

    async def page_delete(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            self_uin = await self.session.get_uin()
            if int(post.uin) != int(self_uin):
                raise RuntimeError("只能删除自己发布的说说")
            resp = await self.qzone.delete(str(post.tid or ""))
            if not resp.ok:
                raise RuntimeError(resp.message or "删除失败")
            return {"ok": True, "data": {}, "message": "说说已删除。"}

        return await self._page_json(handler)

    async def page_upload_media(self):
        async def handler():
            if _quart_request is None:
                raise RuntimeError("当前环境不支持上传")
            files = await _quart_request.files
            upload = files.get("file") or files.get("image") or files.get("media")
            if upload is None:
                raise RuntimeError("没有收到图片文件")
            data = await upload.read()
            return {
                "ok": True,
                "data": {
                    "media": {
                        "kind": "image",
                        "name": upload.filename or "image.jpg",
                        "source": f"base64://{base64.b64encode(data).decode('ascii')}",
                        "size": len(data),
                        "mime_type": getattr(upload, "content_type", "")
                        or "image/jpeg",
                    }
                },
            }

        return await self._page_json(handler)

    async def terminate(self):
        """插件卸载时"""
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        if self.auto_publish:
            await self.auto_publish.terminate()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def prob_read_feed(self, event: AiocqhttpMessageEvent):
        """监听消息"""
        if not self.cfg.client:
            self.cfg.client = event.bot
            logger.debug("QQ空间所需的 CQHttp 客户端已初始化")

        # 按概率触发点赞+评论
        sender_id = event.get_sender_id()
        if (
            not self.cfg.source.is_ignore_user(sender_id)
            and random.random() < self.cfg.trigger.read_prob
        ):
            target_id = event.get_sender_id()
            posts = await self.service.query_feeds(
                target_id=target_id, pos=0, num=1, no_self=True, no_commented=True
            )
            for post in posts:
                try:
                    await self.service.comment_posts(post, event=event)
                    if self.cfg.trigger.like_when_comment:
                        await self.service.like_posts(post)
                    await self.sender.send_post(
                        event,
                        post,
                        message="触发读说说",
                        send_admin=self.cfg.trigger.send_admin,
                    )
                except Exception as e:
                    logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看访客")
    async def view_visitor(self, event: AiocqhttpMessageEvent):
        """查看访客"""
        try:
            msg = await self.service.view_visitor()
            await self.sender.send_msg(event, msg)
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    async def _get_posts(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str | None = None,
        with_detail: bool = False,
        no_commented=False,
        no_self=False,
    ) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None

        if target_id:
            self.cfg.remove_ignore_users(target_id)
        try:
            logger.debug(
                f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}"
            )
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=pos,
                num=num,
                with_detail=with_detail,
                no_commented=no_commented,
                no_self=no_self,
            )
            if not posts:
                await event.send(event.plain_result("查询结果为空"))
                event.stop_event()
            return posts
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)
            event.stop_event()
            return []

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent):
        """
        看说说 <@群友> <序号>
        """
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            await self.sender.send_post(event, post)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("评说说", alias={"评论说说", "读说说"})
    async def comment_feed(self, event: AiocqhttpMessageEvent):
        """评说说 <序号/范围>"""
        posts = await self._get_posts(event, no_commented=True, no_self=True)
        for post in posts:
            try:
                await self.service.comment_posts(post, event=event)
                msg = "已评论"
                if self.cfg.trigger.like_when_comment:
                    await self.service.like_posts(post)
                    msg += "并点赞"
                await self.sender.send_post(event, post, message=msg)
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("赞说说")
    async def like_feed(self, event: AiocqhttpMessageEvent):
        """赞说说 <序号/范围>"""
        posts = await self._get_posts(event)
        for post in posts:
            try:
                await self.service.like_posts(post)
                await self.sender.send_post(event, post, message="已点赞")
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        """发说说 <内容> <图片>, 由用户指定内容"""
        text = event.message_str.partition(" ")[2]
        images = await get_image_urls(event)
        try:
            if self.review.enabled("command"):
                msg = await self.review.submit(
                    event=event, text=text, images=images, source="command"
                )
                yield event.plain_result(msg)
                event.stop_event()
                return
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            event.stop_event()
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("写说说", alias={"写稿"})
    async def write_feed(self, event: AiocqhttpMessageEvent):
        """写说说 <主题> <图片>, 由AI写完后管理员用‘通过稿件 ID’命令发布"""
        group_id = event.get_group_id()
        topic = event.message_str.partition(" ")[2]
        try:
            text = await self.llm.generate_post(
                group_id=group_id, topic=topic, event=event
            )
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
            return
        images = await get_image_urls(event)
        if not text and not images:
            yield event.plain_result("说说生成失败")
            return
        self_id = event.get_self_id()
        post = Post(
            uin=int(self_id),
            text=text or "",
            images=images,
            status=PENDING_BOT_STATUS,
        )
        await self.db.save(post)
        await self.sender.send_post(event, post)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AiocqhttpMessageEvent):
        """删说说 <稿件ID>"""
        posts = await self._get_posts(event, target_id=event.get_self_id())
        for post in posts:
            try:
                await self.sender.send_post(event, post, message="已删除说说")
                await self.service.delete_post(post)
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("回评", alias={"回复评论"})
    async def reply_comment(
        self, event: AiocqhttpMessageEvent, post_id: int = -1, comment_index: int = -1
    ):
        """回评 <稿件ID> <评论序号>, 默认回复最后一条非己评论"""
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return
        try:
            await self.service.reply_comment(post, index=comment_index, event=event)
            await self.sender.send_post(event, post, message="已回复评论")
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)

    @filter.command("投稿")
    async def contribute_post(self, event: AiocqhttpMessageEvent):
        """投稿 <内容> <图片>"""
        await self.campus_wall.contribute(event)

    @filter.command("匿名投稿")
    async def anon_contribute_post(self, event: AiocqhttpMessageEvent):
        """匿名投稿 <内容> <图片>"""
        await self.campus_wall.contribute(event, anon=True)

    @filter.command("撤稿")
    async def recall_post(self, event: AiocqhttpMessageEvent):
        """删除稿件 <稿件ID>"""
        async for msg in self.campus_wall.delete(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看稿", alias={"查看稿件"})
    async def view_post(self, event: AiocqhttpMessageEvent):
        "查看稿件 <稿件ID>, 默认最新稿件"
        async for msg in self.campus_wall.view(event):
            yield msg

    async def _peek_post(self, event: AiocqhttpMessageEvent) -> Post | None:
        """按命令里的稿件 ID 预读稿件, 用于区分机器人待审稿件与表白墙投稿。"""
        args = (event.message_str or "").split()
        raw_id = args[1] if len(args) >= 2 else "-1"
        if not raw_id.lstrip("-").isdigit():
            return None
        try:
            return await self.db.get(int(raw_id))
        except Exception:
            return None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("待审", alias={"待审核", "审核列表"})
    async def list_pending_posts(self, event: AiocqhttpMessageEvent):
        """待审: 查看等待审核的机器人说说"""
        yield event.plain_result(await self.review.list_pending_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("过稿", alias={"通过稿件", "通过投稿"})
    async def approve_post(self, event: AiocqhttpMessageEvent):
        """通过稿件 <稿件ID>: 表白墙投稿与机器人待审说说共用"""
        post = await self._peek_post(event)
        if post is not None and self.review.is_bot_draft(post):
            yield event.plain_result(await self.review.approve(event, post))
            event.stop_event()
            return
        async for msg in self.campus_wall.approve(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒稿", alias={"拒绝稿件", "拒绝投稿"})
    async def reject_post(self, event: AiocqhttpMessageEvent):
        """拒绝稿件 <稿件ID> <原因>"""
        post = await self._peek_post(event)
        if post is not None and self.review.is_bot_draft(post):
            yield event.plain_result(await self.review.reject(event, post))
            event.stop_event()
            return
        async for msg in self.campus_wall.reject(event):
            yield msg

    @filter.llm_tool()
    async def llm_view_feed(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
        tid: str | None = None,
        pos: int = 0,
        content: str = "",
        like: bool = False,
        reply: bool = False,
    ):
        """
        查看、点赞、评论某位用户QQ空间的某条说说、动态。要发自己写的评论内容，用 llm_comment_post，或在本工具同时传 content。查看机器人自己的空间时 user_id 填 self
        Args:
            user_id(string): 目标QQ号，如(12345678)；填 self/自己/我 表示机器人自己的空间；不填则默认当前聊天用户
            tid(string): 说说ID。填了就直接定位这一条说说（忽略pos），可从说说链接或本工具返回里取
            pos(number): 要查询的说说序号, 默认为0表示最新（只在没填tid时生效）
            content(string): 要发表的评论内容。reply=True 时必须传它，否则插件会自己生成一段，内容就不是你写的了；正文里的 @昵称 / @QQ号 会自动转成真 @ 提及，不知道昵称时可以写 @sender 指代当前聊天对象
            like(boolean): 是否点赞
            reply(boolean): 是否评论
        """
        try:
            if not self._is_admin(event):
                return "无权限：QQ空间操作仅限管理员使用。"
            user_id = await self._resolve_target_uin(user_id, event)
            logger.debug(f"正在查询用户（{user_id}）的说说 tid={tid} pos={pos}")

            if tid:
                # 精确到说说，避免 pos 漂移评论到别的说说
                post = await self.service.fetch_post_detail(user_id, tid)
            else:
                posts = await self.service.query_feeds(
                    target_id=user_id,
                    pos=pos,
                    num=1,
                    with_detail=True,
                )
                if not posts:
                    return "查询结果为空"
                post = posts[0]

            # 执行动作
            msg = ""

            if like and reply:
                await self.service.comment_posts(
                    post, event=event, content=content or None
                )
                await self.service.like_posts(post)
                msg = "已评论并点赞"
            elif reply:
                await self.service.comment_posts(
                    post, event=event, content=content or None
                )
                msg = "已评论"
            elif like:
                await self.service.like_posts(post)
                msg = "已点赞"

            # 发送展示
            await self.sender.send_post(event, post, message=msg)

            # 把 tid 带回去，模型下次可以直接用它精确定位这条说说
            head = f"{msg}\nuin={post.uin} tid={post.tid}"
            if not (like or reply):
                head += (
                    f'\n提示：要评论这条，调用 llm_comment_post(user_id="{post.uin}", '
                    f'tid="{post.tid}", content="<你要说的话>")'
                )
            return head + "\n" + post.text + "\n" + "\n".join(post.images)

        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def llm_comment_post(
        self,
        event: AiocqhttpMessageEvent,
        content: str = "",
        user_id: str | None = None,
        tid: str | None = None,
        pos: int = 0,
        like: bool = False,
    ):
        """
        在QQ空间的某条说说下发表评论，评论内容由你指定，不会让插件另生成。评论机器人自己发的说说时 user_id 填 self；不知道 tid 就先调用 llm_view_feed(user_id="self") 拿 tid
        Args:
            content(string): 要发送的评论内容，原样发出；正文里的 @昵称 / @QQ号 会自动转成 QQ 空间的真 @ 提及，不知道对方昵称时可以写 @sender 指代当前聊天对象
            user_id(string): 说说所属的QQ号；填 self/自己/我 表示机器人自己的空间；默认为当前聊天用户
            tid(string): 说说的ID。填了就精确评论这一条（忽略pos），优先级最高
            pos(number): 没有tid时，评论第几条说说，0表示最新
            like(boolean): 是否顺便点赞
        """
        try:
            if not self._is_admin(event):
                return "无权限：QQ空间操作仅限管理员使用。"
            content = str(content or "").strip()
            if not content:
                return "评论内容不能为空"
            user_id = await self._resolve_target_uin(user_id, event)

            if tid:
                post = await self.service.fetch_post_detail(user_id, tid)
            else:
                posts = await self.service.query_feeds(
                    target_id=user_id,
                    pos=pos,
                    num=1,
                    with_detail=True,
                )
                if not posts:
                    return "查询结果为空"
                post = posts[0]

            await self.service.comment_posts(post, event=event, content=content)
            msg = "已评论"
            if like:
                await self.service.like_posts(post)
                msg += "并点赞"
            await self.sender.send_post(event, post, message=msg)
            if tid:
                return f"{msg}\nuin={post.uin} tid={post.tid}\n{post.text}"
            # 没有 tid 时是按序号猜的，结果里明确提示，方便模型自己纠正
            return (
                f"{msg}\n⚠未提供 tid，按第 {pos} 条定位，可能不是目标说说；"
                f"实际定位到 uin={post.uin} tid={post.tid}\n{post.text}"
            )
        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """
        写一篇说说并发布到QQ空间。插件开启审核时, 说说会先提交给管理员过稿, 审核通过后才真正发布, 此时不要声称已经发布
        Args:
            text(string): 要发布的说说内容
            get_image(boolean): 是否获取当前对话中的图片附加到说说里, 默认为True
        """
        try:
            if not self._is_admin(event):
                return "无权限：QQ空间操作仅限管理员使用。"
            images = await get_image_urls(event) if get_image else []
            if self.review.enabled("llm"):
                return await self.review.submit(
                    event=event, text=text, images=images, source="llm"
                )
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            return "已发布说说到QQ空间: \n" + post.text + "\n" + "\n".join(post.images)
        except Exception as e:
            logger.error(e)
            return str(e)
