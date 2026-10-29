from aiocqhttp import CQHttp

from astrbot.api import logger
from astrbot.core.message.components import BaseMessageComponent, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .model import Post
from .renderer import BuiltinQzoneCardRenderer


class Sender:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.renderer = BuiltinQzoneCardRenderer(config)

    async def _post_to_seg(self, post: Post) -> BaseMessageComponent:
        image_path = await self.renderer.render_post(post)
        if image_path:
            return Image.fromFileSystem(str(image_path))
        return Plain(post.to_str())

    async def _text_to_seg(self, text: str) -> BaseMessageComponent:
        image_path = await self.renderer.render_text(text)
        if image_path:
            return Image.fromFileSystem(str(image_path))
        return Plain(text)

    async def _send_chain(
        self,
        event: AstrMessageEvent,
        *segments: BaseMessageComponent,
    ) -> None:
        await event.send(event.chain_result(list(segments)))

    async def _send_to_admins(self, client: CQHttp, obmsg: list[dict]):
        for admin_id in self.cfg.admins_id:
            if admin_id.isdigit():
                try:
                    await client.send_private_msg(user_id=int(admin_id), message=obmsg)
                except Exception as e:
                    logger.error(f"无法反馈管理员：{e}")

    async def _send_to_manage_group(self, client: CQHttp, obmsg: list[dict]) -> bool:
        try:
            await client.send_group_msg(
                group_id=int(self.cfg.manage_group), message=obmsg
            )
            return True
        except Exception as e:
            logger.error(f"无法反馈管理群：{e}")
            return False

    async def _send_to_user(self, client: CQHttp, user_id: int, obmsg: list[dict]):
        try:
            await client.send_private_msg(user_id=int(user_id), message=obmsg)
        except Exception as e:
            logger.error(f"无法通知用户{user_id}：{e}")

    async def _send_to_group(self, client: CQHttp, group_id: int, obmsg: list[dict]):
        try:
            await client.send_group_msg(group_id=int(group_id), message=obmsg)
        except Exception as e:
            logger.error(f"无法通知群聊{group_id}：{e}")

    async def send_admin_post(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        message: str = "",
    ):
        """通知管理群或管理员"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return

        chain = []
        if message:
            chain.append(Plain(message))
        post_seg = await self._post_to_seg(post)
        chain.append(post_seg)

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        succ = False
        if self.cfg.manage_group:
            succ = await self._send_to_manage_group(client, obmsg)
        if not succ and self.cfg.admins_id:
            await self._send_to_admins(client, obmsg)

    async def send_admin_text(self, text: str, *, client: CQHttp | None = None) -> bool:
        """给管理群 / 管理员发一条纯文本通知（不渲染卡片）"""
        client = client or self.cfg.client
        if not client or not (text or "").strip():
            return False

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(
            MessageChain([Plain(text)])
        )
        if self.cfg.manage_group and await self._send_to_manage_group(client, obmsg):
            return True
        if self.cfg.admins_id:
            await self._send_to_admins(client, obmsg)
            return True
        return False

    async def send_comment_notice(
        self,
        post: Post,
        content: str,
        *,
        source: str = "自动评论",
        event: AstrMessageEvent | None = None,
    ) -> bool:
        """通知管理群 / 管理员：机器人自动评论了谁的哪条说说"""
        client = getattr(event, "bot", None) or self.cfg.client
        author = (post.name or "").strip() or str(post.uin)
        lines = [f"【{source}】机器人已自动评论"]

        try:
            group_id = str(event.get_group_id() or "") if event is not None else ""
        except Exception:
            group_id = ""
        if group_id:
            lines.append(f"来源群：{group_id}")

        lines.append(f"对象：{author}（{post.uin}）")
        if content:
            lines.append(f"评论：{content}")

        preview = " ".join((post.text or "").split())
        if preview:
            lines.append(f"原文：{preview[:60]}{'…' if len(preview) > 60 else ''}")

        return await self.send_admin_text("\n".join(lines), client=client)

    async def send_post_to(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        group_id: int | None = None,
        user_id: int | None = None,
        message: str = "",
    ) -> bool:
        """把稿件卡片发到指定群聊或私聊（不依赖 event，供审核结果回发等场景使用）"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return False
        if not group_id and not user_id:
            return False

        chain = []
        if message:
            chain.append(Plain(message))
        chain.append(await self._post_to_seg(post))

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        if group_id:
            await self._send_to_group(client, int(group_id), obmsg)
        else:
            await self._send_to_user(client, int(user_id or 0), obmsg)
        return True

    async def send_user_post(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        message: str = "",
    ):
        """通知投稿者"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return

        chain = []
        if message:
            chain.append(Plain(message))
        post_seg = await self._post_to_seg(post)
        chain.append(post_seg)

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        if post.gin:
            await self._send_to_group(client, post.gin, obmsg)
        elif post.uin:
            await self._send_to_user(client, post.uin, obmsg)

    async def send_post(
        self,
        event: AstrMessageEvent,
        post: Post,
        *,
        message: str = "",
        send_admin: bool = False,
    ):
        if send_admin and self.cfg.admin_id:
            event.message_obj.group_id = None  # type: ignore
            event.message_obj.sender.user_id = self.cfg.admin_id

        chain = []

        if message:
            chain.append(Plain(message))

        chain.append(await self._post_to_seg(post))

        await event.send(event.chain_result(chain))

    async def send_msg(
        self,
        event: AstrMessageEvent,
        message: str = "",
    ):
        seg = await self._text_to_seg(message)
        await self._send_chain(event, seg)
