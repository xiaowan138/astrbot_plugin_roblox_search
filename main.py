"""
main.py —— Roblox 全功能查询插件（AstrBot 版）

由 NoneBot2 插件 nonebot_plugin_roblox_search (v1.3.5) 迁移而来，
仅替换框架层（NoneBot → AstrBot），全部查询功能、API、输出格式保持不变。

功能命令（均可带 / 前缀触发，无斜杠触发可在配置中关闭）：
    菜单 / 帮助 / menu          查看功能菜单（图片）
    用户名搜索 [用户名]          根据用户名查询用户完整资料
    用户ID搜索 [数字ID]          按用户ID查询用户完整资料
    群组名搜索 [群组名]          模糊搜索群组并展示详情
    群组ID搜索 [数字ID]          查询群组详情与职位列表
    游戏名搜索 [游戏名]          搜索游戏、在线人数、访问量
    游戏ID搜索 [数字ID]          查询游戏详情与公开服务器列表
    获取好友列表 [用户ID]        读取用户前10位好友
    获取粉丝列表 [用户ID]        读取用户前10位粉丝
    获取关注列表 [用户ID]        读取用户前10位关注
"""

import asyncio
import os
import tempfile
import time
import traceback
from datetime import datetime

from dateutil.relativedelta import relativedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .roblox_api import (
    close_client,
    download_image,
    get_avatar_url,
    get_follower_count,
    get_followers,
    get_following_count,
    get_followings,
    get_friend_count,
    get_friends,
    get_game_icon,
    get_game_info,
    get_game_servers,
    get_group_icon,
    get_group_info,
    get_group_roles,
    get_headshot_url,
    get_user_details,
    get_user_groups,
    get_user_presence,
    search_game,
    search_group,
    search_user,
)
from .render_utils import menu_to_image

# 非白名单群的拒绝提示（与原插件一致）
WHITELIST_MSG = "此群未获得账号所有者的允许，未开放此群白名单，暂时不开使用，请联系账号所有者"


def _parse_param(event: AstrMessageEvent, keyword: str) -> str:
    """从消息中剥离命令关键词，返回剩余参数。

    兼容两种情况：消息含完整命令（"/用户名搜索 Roblox" / "用户名搜索 Roblox"），
    或框架已剥离命令（只剩 "Roblox"）。
    """
    msg = event.message_str.strip()
    if msg.startswith("/"):
        msg = msg[1:]
    if msg.startswith(keyword):
        return msg[len(keyword):].strip()
    return msg


def _parse_date(s: str):
    """解析 Roblox API 的 ISO 时间字符串，失败返回 None"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", ""))
        except Exception:
            return None


def _calc_age(created_dt):
    """计算注册时长，返回 (注册日期字符串, 时长描述)；传入 None 返回两个空串"""
    if not created_dt:
        return "", ""
    created_date = created_dt.strftime("%Y-%m-%d")
    delta = relativedelta(datetime.now(), created_dt)
    total_days = (datetime.now() - created_dt).days
    age_info = f"{delta.years}年{delta.months}月{delta.days}天（共{total_days}天）"
    return created_date, age_info


def _parse_presence(status):
    """解析在线状态，返回 (在线状态文本, 位置)"""
    online_status = "离线"
    location = "无"
    if status and isinstance(status, dict):
        ptype = status.get("userPresenceType")
        if ptype == 2:
            online_status = "在线"
        elif ptype == 3:
            online_status = "游戏中"
        elif ptype == 4:
            online_status = "工作室中"
        if status.get("lastLocation"):
            location = status["lastLocation"]
    return online_status, location


async def _download_to_temp(url: str, suffix: str = ".png") -> str | None:
    """下载图片到临时文件，返回路径；失败返回 None"""
    try:
        data = await download_image(url)
        if not data:
            return None
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


async def _menu_to_temp() -> str | None:
    """生成菜单图片并写入临时文件，返回路径；失败返回 None"""
    try:
        data = await menu_to_image()
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        logger.error(f"[Roblox] 菜单生成失败: {e}")
        return None


class RobloxSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 群白名单：留空 = 所有群全开放；配置后仅白名单内群可用查询功能（菜单始终可用），私聊不受限制
        raw_whitelist = config.get("search_white_list", []) or []
        self.whitelist = [str(x) for x in raw_whitelist]
        # 是否支持无斜杠触发命令
        self.allow_no_slash = bool(config.get("allow_no_slash_command", True))
        self._no_slash_handlers = {
            "用户名搜索": self.username_search,
            "用户ID搜索": self.user_id_search,
            "群组名搜索": self.group_name_search,
            "群组ID搜索": self.group_id_search,
            "游戏名搜索": self.game_name_search,
            "游戏ID搜索": self.game_id_search,
            "获取好友列表": self.friends_list,
            "获取粉丝列表": self.followers_list,
            "获取关注列表": self.followings_list,
        }
        if self.whitelist:
            logger.info(f"[Roblox全功能查询] 已加载群白名单: {self.whitelist}（仅白名单群可用查询功能）")
        else:
            logger.info("[Roblox全功能查询] 未配置群白名单，所有群均可使用查询功能")

    def _check_whitelist(self, event: AstrMessageEvent) -> bool:
        """私聊放行；群聊时未配置白名单则放行，配置了则须在白名单内"""
        group_id = str(event.message_obj.group_id or "").strip()
        if not group_id:
            return True  # 私聊放行
        if not self.whitelist:
            return True  # 未配置白名单 = 全开放
        return group_id in self.whitelist

    # ============ 菜单 ============

    @filter.command("菜单", alias={"帮助", "menu"})
    async def menu(self, event: AstrMessageEvent):
        '''查看功能菜单'''
        path = await _menu_to_temp()
        if path:
            yield event.chain_result([Image.fromFileSystem(path)])
        else:
            yield event.plain_result("菜单生成失败，请稍后重试")

    # ============ 用户查询 ============

    @filter.command("用户名搜索")
    async def username_search(self, event: AstrMessageEvent):
        '''根据用户名查询 Roblox 用户完整资料'''
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        username = _parse_param(event, "用户名搜索")
        if not username:
            yield event.plain_result("请输入用户名，例：/用户名搜索 Roblox")
            return
        user_info = await search_user(username)
        if not user_info:
            yield event.plain_result("未找到该用户，请检查用户名是否正确！")
            return
        async for res in self._user_query(event, user_info["id"], "用户名搜索"):
            yield res

    @filter.command("用户ID搜索")
    async def user_id_search(self, event: AstrMessageEvent):
        '''按用户ID查询 Roblox 用户完整资料'''
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        uid_str = _parse_param(event, "用户ID搜索")
        if not uid_str or not uid_str.isdigit():
            yield event.plain_result("请输入有效的用户ID（纯数字），例：/用户ID搜索 123456789")
            return
        uid = int(uid_str)
        async for res in self._user_query(event, uid, "用户ID搜索"):
            yield res

    async def _user_query(self, event: AstrMessageEvent, user_id: int, source: str):
        """用户详情查询（用户名搜索 / 用户ID搜索共用）"""
        yield event.plain_result("稍等，正在查询Roblox用户信息...")
        total_start = time.time()
        try:
            details = await get_user_details(user_id)
            if not details or not details.get("name"):
                yield event.plain_result("未找到该用户，请检查用户名或用户ID是否正确！")
                return

            raw_name = details.get("name", "")
            display_name = details.get("displayName", raw_name)
            created_raw = details.get("created", "")
            is_banned = details.get("isBanned", False)
            description = details.get("description", "")

            results = await asyncio.gather(
                get_user_presence(user_id),
                get_user_groups(user_id),
                get_friend_count(user_id),
                get_follower_count(user_id),
                get_following_count(user_id),
                get_avatar_url(user_id),
                get_headshot_url(user_id),
                return_exceptions=True,
            )

            def _safe(idx, default):
                r = results[idx]
                return default if isinstance(r, Exception) else r

            status = _safe(0, None)
            groups = _safe(1, []) or []
            friend_count = _safe(2, 0)
            follower_count = _safe(3, 0)
            following_count = _safe(4, 0)
            avatar_url = _safe(5, "") or ""
            headshot_url = _safe(6, "") or ""

            online_status, location = _parse_presence(status)
            created_date, age_info = _calc_age(_parse_date(created_raw))

            title = "📄 Roblox 用户信息查询" if source == "用户名搜索" else "📄 Roblox 用户信息查询（ID）"
            output = f"{title}\n\n"
            output += f"👤 用户名：{raw_name}\n"
            output += f"🏷️ 展示名：{display_name}\n"
            output += f"🆔 用户ID：{user_id}\n"
            if created_date:
                output += f"📅 注册日期：{created_date}\n"
                output += f"⏳ 注册时长：{age_info}\n"
            output += f"👥 好友：{friend_count} | 关注：{following_count} | 粉丝：{follower_count}\n"
            output += f"🟢 在线状态：{online_status}\n"
            output += f"📍 当前位置：{location}\n"
            output += f"🚫 账号封禁：{'是' if is_banned else '否'}\n"
            if description:
                output += f"\n📝 用户简介：\n{description}\n"
            if groups:
                output += f"\n🏠 已加入群组(前5个)：\n"
                for idx, group in enumerate(groups[:5], 1):
                    group_name = group.get("group", {}).get("name", "未知")
                    role = group.get("role", {}).get("name", "未知")
                    gid = group.get("group", {}).get("id", 0)
                    output += f"{idx}) {group_name}｜职位:{role}\n"
                    output += f"   群组ID：{gid}\n"

            # 头像框 + 形象图在前，文本在后（与原插件顺序一致）
            chain = []
            for url in (headshot_url, avatar_url):
                path = await _download_to_temp(url) if url else None
                if path:
                    chain.append(Image.fromFileSystem(path))
            chain.append(Plain("\u200b" + output))
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"查询失败：{str(e)}")
        finally:
            logger.info(f"[Roblox] {source} 耗时: {time.time() - total_start:.2f}s")

    # ============ 群组查询 ============

    @filter.command("群组名搜索")
    async def group_name_search(self, event: AstrMessageEvent):
        '''根据群组名模糊搜索群组并展示详情'''
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        name = _parse_param(event, "群组名搜索")
        if not name:
            yield event.plain_result("请输入群组名，例：/群组名搜索 Roblox")
            return
        yield event.plain_result("稍等，正在搜索群组...")
        try:
            search_result = await search_group(name)
            groups = search_result.get("data", []) if search_result else []
            if not groups:
                yield event.plain_result("未找到匹配的群组，请检查群组名是否正确！")
                return

            group = groups[0]
            gid = group.get("id", 0)
            group_info, icon_url = await asyncio.gather(
                get_group_info(gid), get_group_icon(gid), return_exceptions=True)
            group_info = {} if isinstance(group_info, Exception) or not group_info else group_info
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")

            name_out = group_info.get("name", "未知")
            description = (group_info.get("description", "") or "").strip() or "无描述"
            member_count = group_info.get("memberCount", 0)
            owner = group_info.get("owner", {}) or {}
            owner_name = owner.get("name", "未知")
            is_public = group_info.get("publicEntryAllowed", False)
            create_dt = _parse_date(group_info.get("created", ""))

            output = f"🏠 Roblox 群组搜索结果\n\n"
            output += f"🆔 群组ID：{gid}\n"
            output += f"📛 群组名：{name_out}\n"
            output += f"👥 成员数量：{member_count:,}\n"
            output += f"👤 群主：{owner_name}\n"
            output += f"📅 创建时间：{create_dt.strftime('%Y-%m-%d') if create_dt else '未知'}\n"
            output += f"🌐 是否公开：{'是' if is_public else '否'}\n\n"
            output += f"📝 群组描述：\n{description[:300]}{'......' if len(description) > 300 else ''}"

            chain = []
            icon_path = await _download_to_temp(icon_url) if icon_url else None
            if icon_path:
                chain.append(Image.fromFileSystem(icon_path))
            chain.append(Plain("\u200b" + output))
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"查询失败：{str(e)}")

    @filter.command("群组ID搜索")
    async def group_id_search(self, event: AstrMessageEvent):
        '''根据群组ID查询群组详情与职位列表'''
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        gid_str = _parse_param(event, "群组ID搜索")
        if not gid_str or not gid_str.isdigit():
            yield event.plain_result("请输入有效的群组ID（纯数字），例：/群组ID搜索 123456")
            return
        gid = int(gid_str)
        yield event.plain_result("稍等，正在查询群组信息...")
        try:
            group_info, roles, icon_url = await asyncio.gather(
                get_group_info(gid), get_group_roles(gid), get_group_icon(gid),
                return_exceptions=True)
            group_info = {} if isinstance(group_info, Exception) or not group_info else group_info
            roles = [] if isinstance(roles, Exception) else (roles or [])
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")
            if not group_info:
                yield event.plain_result("未找到该群组，请检查群组ID是否正确！")
                return

            name_out = group_info.get("name", "未知")
            description = (group_info.get("description", "") or "").strip() or "无描述"
            member_count = group_info.get("memberCount", 0)
            owner = group_info.get("owner", {}) or {}
            owner_name = owner.get("name", "未知")
            is_public = group_info.get("publicEntryAllowed", False)
            create_dt = _parse_date(group_info.get("created", ""))

            output = f"🏠 Roblox 群组详情查询\n\n"
            output += f"🆔 群组ID：{gid}\n"
            output += f"📛 群组名：{name_out}\n"
            output += f"👥 成员数量：{member_count:,}\n"
            output += f"👤 群主：{owner_name}\n"
            output += f"📅 创建时间：{create_dt.strftime('%Y-%m-%d') if create_dt else '未知'}\n"
            output += f"🌐 是否公开：{'是' if is_public else '否'}\n\n"
            output += f"📝 群组描述：\n{description[:200]}{'......' if len(description) > 200 else ''}"

            if roles:
                output += f"\n\n💼 职位列表（前5个）：\n"
                for idx, role in enumerate(roles[:5], 1):
                    role_name = role.get("name") or role.get("displayName") or "未知"
                    role_count = role.get("memberCount") or role.get("count") or 0
                    output += f"{idx}) {role_name}｜成员数:{role_count}\n"

            chain = []
            icon_path = await _download_to_temp(icon_url) if icon_url else None
            if icon_path:
                chain.append(Image.fromFileSystem(icon_path))
            chain.append(Plain("\u200b" + output))
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"查询失败：{str(e)}")

    # ============ 游戏查询 ============

    @filter.command("游戏名搜索")
    async def game_name_search(self, event: AstrMessageEvent):
        '''根据游戏名搜索游戏'''
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        name = _parse_param(event, "游戏名搜索")
        if not name:
            yield event.plain_result("请输入游戏名，例：/游戏名搜索 Adopt Me")
            return
        yield event.plain_result("稍等，正在搜索游戏...")
        try:
            search_result = await search_game(name)
            games = search_result.get("data", []) if search_result else []
            if not games:
                yield event.plain_result("未找到匹配的游戏，请检查游戏名是否正确！")
                return

            game = games[0]
            place_id = game.get("placeId", 0)
            game_id = game.get("id", 0)
            game_info, icon_url = await asyncio.gather(
                get_game_info(place_id), get_game_icon(game_id), return_exceptions=True)
            game_info = {} if isinstance(game_info, Exception) or not game_info else game_info
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")

            game_data = game_info.get("data", []) if isinstance(game_info, dict) else []
            game_detail = game_data[0] if game_data else {}

            name_out = game_detail.get("name", game.get("name", "未知"))
            description = (game_detail.get("description", "") or "").strip() or "无描述"
            creator = game_detail.get("creator", {}) or {}
            creator_name = creator.get("name", game.get("creatorName", "未知"))
            playing = game_detail.get("playing", 0)
            visits = game_detail.get("visits", 0)
            favorites = game_detail.get("favorites", 0)
            create_dt = _parse_date(game_detail.get("created", ""))
            update_dt = _parse_date(game_detail.get("updated", ""))

            output = f"🎮 Roblox 游戏搜索结果\n\n"
            output += f"🆔 游戏ID：{game_id}\n"
            output += f"📍 地点ID：{place_id}\n"
            output += f"📛 游戏名：{name_out}\n"
            output += f"👤 开发者：{creator_name}\n"
            output += f"👥 当前游玩：{playing:,}\n"
            output += f"👁️ 总访问量：{visits:,}\n"
            output += f"❤️ 收藏数：{favorites:,}\n"
            output += f"📅 创建时间：{create_dt.strftime('%Y-%m-%d') if create_dt else '未知'}\n"
            output += f"🔄 更新时间：{update_dt.strftime('%Y-%m-%d') if update_dt else '未知'}\n\n"
            output += f"📝 游戏描述：\n{description[:300]}{'......' if len(description) > 300 else ''}"

            chain = []
            icon_path = await _download_to_temp(icon_url) if icon_url else None
            if icon_path:
                chain.append(Image.fromFileSystem(icon_path))
            chain.append(Plain("\u200b" + output))
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"查询失败：{str(e)}")

    @filter.command("游戏ID搜索")
    async def game_id_search(self, event: AstrMessageEvent):
        '''根据游戏ID查询游戏详情与公开服务器列表'''
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        gid_str = _parse_param(event, "游戏ID搜索")
        if not gid_str or not gid_str.isdigit():
            yield event.plain_result("请输入有效的游戏ID（纯数字），例：/游戏ID搜索 292439477")
            return
        gid = int(gid_str)
        yield event.plain_result("稍等，正在查询游戏信息...")
        try:
            game_info, icon_url, servers = await asyncio.gather(
                get_game_info(gid), get_game_icon(gid), get_game_servers(gid),
                return_exceptions=True)
            game_info = {} if isinstance(game_info, Exception) or not game_info else game_info
            icon_url = "" if isinstance(icon_url, Exception) else (icon_url or "")
            servers = [] if isinstance(servers, Exception) else (servers or [])

            game_data = game_info.get("data", []) if isinstance(game_info, dict) else []
            game_detail = game_data[0] if game_data else {}
            if not game_detail:
                yield event.plain_result("未找到该游戏，请检查游戏ID是否正确！")
                return

            name_out = game_detail.get("name", "未知")
            description = (game_detail.get("description", "") or "").strip() or "无描述"
            creator = game_detail.get("creator", {}) or {}
            creator_name = creator.get("name", "未知")
            playing = game_detail.get("playing", 0)
            visits = game_detail.get("visits", 0)
            favorites = game_detail.get("favorites", 0)
            create_dt = _parse_date(game_detail.get("created", ""))
            update_dt = _parse_date(game_detail.get("updated", ""))

            output = f"🎮 Roblox 游戏详情查询\n\n"
            output += f"🆔 游戏ID：{gid}\n"
            output += f"📛 游戏名：{name_out}\n"
            output += f"👤 开发者：{creator_name}\n"
            output += f"👥 当前游玩：{playing:,}\n"
            output += f"👁️ 总访问量：{visits:,}\n"
            output += f"❤️ 收藏数：{favorites:,}\n"
            output += f"📅 创建时间：{create_dt.strftime('%Y-%m-%d') if create_dt else '未知'}\n"
            output += f"🔄 更新时间：{update_dt.strftime('%Y-%m-%d') if update_dt else '未知'}\n\n"
            output += f"📝 游戏描述：\n{description[:200]}{'......' if len(description) > 200 else ''}"

            if servers:
                output += f"\n\n🖥️ 公开服务器（前3个）：\n"
                for idx, server in enumerate(servers[:3], 1):
                    server_id = server.get("id", "未知")
                    s_playing = server.get("playing", 0)
                    max_players = server.get("maxPlayers", "?")
                    ping = server.get("ping", "?")
                    output += f"{idx}) {server_id}｜{s_playing}/{max_players}｜{ping}ms\n"

            chain = []
            icon_path = await _download_to_temp(icon_url) if icon_url else None
            if icon_path:
                chain.append(Image.fromFileSystem(icon_path))
            chain.append(Plain("\u200b" + output))
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"查询失败：{str(e)}")

    # ============ 社交关系查询 ============

    async def _social_list(self, event: AstrMessageEvent, keyword: str, title: str, fetch):
        """社交列表查询（好友/粉丝/关注共用），fetch 为对应的异步查询函数"""
        if not self._check_whitelist(event):
            yield event.plain_result(WHITELIST_MSG)
            return
        uid_str = _parse_param(event, keyword)
        if not uid_str or not uid_str.isdigit():
            yield event.plain_result(f"请输入有效的用户ID（纯数字），例：/{keyword} 123456789")
            return
        uid = int(uid_str)
        yield event.plain_result(f"稍等，正在获取{title}...")
        try:
            items = await fetch(uid, 10)
            if not items:
                yield event.plain_result("未找到该用户的相关列表或用户ID不存在！")
                return
            output = f"👥 用户ID {uid} 的{title}（前10个）\n\n"
            for idx, item in enumerate(items, 1):
                name = item.get("name", "未知")
                display_name = item.get("displayName", "未知")
                iid = item.get("id", 0)
                output += f"{idx}. {name}（{display_name}）\n🆔 ID：{iid}\n\n"
            yield event.plain_result("\u200b" + output.strip())
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"获取失败：{str(e)}")

    @filter.command("获取好友列表")
    async def friends_list(self, event: AstrMessageEvent):
        '''读取用户前10位好友'''
        async for res in self._social_list(event, "获取好友列表", "好友列表", get_friends):
            yield res

    @filter.command("获取粉丝列表")
    async def followers_list(self, event: AstrMessageEvent):
        '''读取用户前10位粉丝'''
        async for res in self._social_list(event, "获取粉丝列表", "粉丝列表", get_followers):
            yield res

    @filter.command("获取关注列表")
    async def followings_list(self, event: AstrMessageEvent):
        '''读取用户前10位关注'''
        async for res in self._social_list(event, "获取关注列表", "关注列表", get_followings):
            yield res

    # ============ 无斜杠命令分发（可在配置中关闭） ============

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def no_slash_dispatch(self, event: AstrMessageEvent):
        '''无斜杠触发支持：如直接发送「用户名搜索 Roblox」即可触发（受 allow_no_slash_command 配置控制）'''
        if not self.allow_no_slash:
            return
        msg = event.message_str.strip()
        if not msg or msg.startswith("/"):
            return  # 斜杠命令由 @filter.command 处理，避免重复
        if msg in ("菜单", "帮助", "menu"):
            async for res in self.menu(event):
                yield res
            event.stop_event()
            return
        for kw, handler in self._no_slash_handlers.items():
            if msg.startswith(kw):
                async for res in handler(event):
                    yield res
                event.stop_event()
                return

    async def terminate(self):
        '''插件卸载/停用时关闭全局 httpx 客户端'''
        await close_client()
