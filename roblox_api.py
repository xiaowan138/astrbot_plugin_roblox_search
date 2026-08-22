"""
roblox_api.py —— Roblox 查询 API 封装（迁移自 nonebot_plugin_roblox_search/http_utils.py）

改动说明：
- 原插件使用同步 `requests` 库，在异步协程中调用会阻塞事件循环；
  此处按 AstrBot 官方建议改用 `httpx.AsyncClient`，并复用原插件的
  请求头、超时、重试、429 限流退避逻辑。
- 数据源为第三方代理 API（默认 `*.rotunnel.com`，非 Roblox 官方接口）。
  代理域名可通过 set_base_domain() / 插件配置 api_base_domain 更换
  （如 roproxy.com、自建反代），rotunnel 挂掉时无需改代码。

错误语义：
- 4xx（400/401/403/404 等）：视为“请求无效 / 资源不存在 / 接口不可用”，返回 None，
  由上层提示“未找到”。
- 网络错误 / 5xx / 429 重试耗尽：抛出 RobloxAPIError，由上层提示“查询失败”。

2026-08-22 实测代理接口状态：
- 正常：用户详情、精确用户名 POST 查询、批量用户查询、在线状态、群组信息/搜索/
  职位、用户群组、好友/粉丝/关注数量、好友列表、全部缩略图、官方徽章
- 不可用（上游 Roblox 侧原因，换代理域名也无法恢复）：
  · games/list（游戏名搜索）→ 404，接口已被 Roblox 下线
  · servers/Public（公开服务器列表）→ 403，endpoint has been disabled
  · followers/followings 列表 → 401，需要登录令牌
  · games?placeIds 参数 → 400（已用 universeIds 双查询兼容）
- 好友列表接口不再返回用户名（name/displayName 为空串），
  get_friends 会自动调用批量用户查询接口补全名字
- users/search 关键词搜索对高频词（如 Roblox）会 504 超时，
  search_user 已改为“精确匹配优先、关键词搜索兜底”
"""

import asyncio
import urllib.parse

import httpx

from astrbot.api import logger

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.roblox.com",
    "Referer": "https://www.roblox.com/",
}

TIMEOUT = 30          # 常规接口超时（秒）
SEARCH_TIMEOUT = 10   # 搜索类接口超时（秒）：高频词搜索在代理上会长时间 504，短超时快速失败
MAX_RETRIES = 3
RETRY_DELAY = 2

# 数据源代理域名（默认 rotunnel.com，可通过插件配置更换，如 roproxy.com）
_BASE_DOMAIN = "rotunnel.com"

_client: httpx.AsyncClient | None = None


class RobloxAPIError(Exception):
    """Roblox API 请求失败（网络错误 / 5xx / 429 重试耗尽）。"""


def set_base_domain(domain: str) -> None:
    """设置数据源代理域名（如 rotunnel.com / roproxy.com / 自建反代域名）。

    容忍用户误带 https:// 前缀或末尾斜杠。
    """
    global _BASE_DOMAIN
    domain = (domain or "").strip()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.rstrip("/")
    if domain and domain != _BASE_DOMAIN:
        _BASE_DOMAIN = domain
        logger.info(f"[Roblox] 数据源代理域名已切换为: {domain}")


def get_client() -> httpx.AsyncClient:
    """获取全局复用的 httpx 异步客户端（懒加载）"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    return _client


async def close_client():
    """关闭全局 httpx 客户端（插件卸载时调用）"""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


async def http_get(url: str, retries: int = MAX_RETRIES, timeout: float = TIMEOUT):
    """GET 请求，带 429 限流退避与失败重试。

    返回解析后的 JSON；4xx 返回 None；网络错误 / 5xx / 429 重试耗尽抛 RobloxAPIError。
    搜索类接口建议传 retries=1, timeout=SEARCH_TIMEOUT，避免长时间等待后才失败。
    """
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = await client.get(url, timeout=timeout)
            if response.status_code == 429:
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    logger.warning(f"[HTTP] 429 Too Many Requests，第{attempt + 1}次重试，等待{wait_time}秒...")
                    await asyncio.sleep(wait_time)
                    continue
                raise RobloxAPIError(f"429 Too Many Requests（已重试{retries}次）：{url}")
            if 400 <= response.status_code < 500:
                # 请求无效 / 资源不存在 / 接口不可用，交由上层按“未找到/不可用”处理
                return None
            response.raise_for_status()
            return response.json()
        except RobloxAPIError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                logger.warning(f"[HTTP] 请求失败，第{attempt + 1}次重试：{e}，等待{wait_time}秒...")
                await asyncio.sleep(wait_time)
                continue
    raise RobloxAPIError(f"Roblox 接口请求失败（已重试{retries}次）：{last_exc}") from last_exc


async def http_post(url: str, data=None, retries: int = MAX_RETRIES, timeout: float = TIMEOUT):
    """POST 请求，带 429 限流退避与失败重试。

    返回解析后的 JSON；4xx 返回 None；网络错误 / 5xx / 429 重试耗尽抛 RobloxAPIError。
    """
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = await client.post(url, json=data, timeout=timeout)
            if response.status_code == 429:
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    logger.warning(f"[HTTP] 429 Too Many Requests，第{attempt + 1}次重试，等待{wait_time}秒...")
                    await asyncio.sleep(wait_time)
                    continue
                raise RobloxAPIError(f"429 Too Many Requests（已重试{retries}次）：{url}")
            if 400 <= response.status_code < 500:
                return None
            response.raise_for_status()
            return response.json()
        except RobloxAPIError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                logger.warning(f"[HTTP] 请求失败，第{attempt + 1}次重试：{e}，等待{wait_time}秒...")
                await asyncio.sleep(wait_time)
                continue
    raise RobloxAPIError(f"Roblox 接口请求失败（已重试{retries}次）：{last_exc}") from last_exc


# ============ 用户相关 API ============

async def search_user(username: str):
    """通过用户名查询用户，返回用户 dict；未找到返回 None。

    优先走 POST /v1/usernames/users 精确匹配（快速稳定，大小写不敏感）；
    未命中再回退 GET /v1/users/search 关键词搜索（高频词在代理上可能 504，
    已用短超时 + 单次尝试控制失败耗时）。
    网络/服务端错误抛 RobloxAPIError，由调用方提示“查询失败”。
    """
    try:
        exact = await http_post(
            f"https://users.{_BASE_DOMAIN}/v1/usernames/users",
            data={"usernames": [username], "excludeBannedUsers": False},
            retries=1, timeout=SEARCH_TIMEOUT,
        )
        if exact and exact.get("data"):
            return exact["data"][0]
    except RobloxAPIError:
        pass  # 精确匹配失败不阻断，回退关键词搜索
    encoded_username = urllib.parse.quote(username)
    data = await http_get(
        f"https://users.{_BASE_DOMAIN}/v1/users/search?keyword={encoded_username}",
        retries=1, timeout=SEARCH_TIMEOUT,
    )
    if data and data.get("data"):
        return data["data"][0]
    return None


async def lookup_users(user_ids: list[int]) -> list[dict]:
    """批量获取用户名/展示名（POST /v1/users，单次最多 100 个）；失败返回空列表。

    好友列表等接口已不再返回用户名，需要用这个接口补全。
    """
    if not user_ids:
        return []
    try:
        data = await http_post(
            f"https://users.{_BASE_DOMAIN}/v1/users",
            data={"userIds": user_ids[:100]},
            retries=1, timeout=SEARCH_TIMEOUT,
        )
        if data and data.get("data"):
            return data["data"]
    except Exception:
        pass
    return []


async def get_user_details(user_id: int):
    """获取用户完整资料；未找到（4xx）返回 None，网络/服务端错误抛 RobloxAPIError"""
    url = f"https://users.{_BASE_DOMAIN}/v1/users/{user_id}"
    return await http_get(url)


async def get_user_badges(user_id: int):
    """获取用户获得的 Roblox 官方徽章列表（返回 list）；失败返回空列表。

    接口返回条目字段：id / name / description / imageUrl（无获得日期）。
    """
    url = f"https://accountinformation.{_BASE_DOMAIN}/v1/users/{user_id}/roblox-badges"
    try:
        data = await http_get(url, retries=1)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


async def get_user_presence(user_id: int):
    """获取用户在线状态；失败返回 None（不影响主查询）"""
    url = f"https://presence.{_BASE_DOMAIN}/v1/presence/users"
    try:
        data = await http_post(url, data={"userIds": [user_id]})
        if data and data.get("userPresences"):
            return data["userPresences"][0]
    except Exception:
        pass
    return None


async def get_user_groups(user_id: int, limit: int = 5):
    """获取用户加入的群组（前 limit 个）；失败返回空列表"""
    url = f"https://groups.{_BASE_DOMAIN}/v1/users/{user_id}/groups/roles?limit={limit}"
    try:
        data = await http_get(url)
        if data:
            return data.get("data", [])
    except Exception:
        pass
    return []


async def get_friend_count(user_id: int):
    """获取用户好友数；失败返回 None（由上层显示“未知”）"""
    url = f"https://friends.{_BASE_DOMAIN}/v1/users/{user_id}/friends/count"
    try:
        data = await http_get(url)
        if data:
            return data.get("count")
    except Exception:
        pass
    return None


async def get_follower_count(user_id: int):
    """获取用户粉丝数；失败返回 None（由上层显示“未知”）"""
    url = f"https://friends.{_BASE_DOMAIN}/v1/users/{user_id}/followers/count"
    try:
        data = await http_get(url)
        if data:
            return data.get("count")
    except Exception:
        pass
    return None


async def get_following_count(user_id: int):
    """获取用户关注数；失败返回 None（由上层显示“未知”）"""
    url = f"https://friends.{_BASE_DOMAIN}/v1/users/{user_id}/followings/count"
    try:
        data = await http_get(url)
        if data:
            return data.get("count")
    except Exception:
        pass
    return None


async def get_avatar_url(user_id: int):
    """获取用户 3D 形象图 URL；失败返回空串"""
    url = f"https://thumbnails.{_BASE_DOMAIN}/v1/users/avatar?userIds={user_id}&size=420x420&format=Png"
    try:
        data = await http_get(url)
        if data and data.get("data"):
            return data["data"][0].get("imageUrl", "")
    except Exception:
        pass
    return ""


async def get_headshot_url(user_id: int):
    """获取用户头像框 URL；失败返回空串"""
    url = f"https://thumbnails.{_BASE_DOMAIN}/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=false"
    try:
        data = await http_get(url)
        if data and data.get("data"):
            return data["data"][0].get("imageUrl", "")
    except Exception:
        pass
    return ""


# ============ 群组相关 API ============

async def search_group(name: str):
    """按名称模糊搜索群组（关键词做 URL 编码，支持中文/空格）"""
    encoded = urllib.parse.quote(name)
    url = f"https://groups.{_BASE_DOMAIN}/v1/groups/search?keyword={encoded}&limit=10"
    return await http_get(url, retries=1, timeout=SEARCH_TIMEOUT)


async def get_group_info(gid: int):
    """获取群组基本信息；未找到（4xx）返回 None，网络/服务端错误抛 RobloxAPIError"""
    url = f"https://groups.{_BASE_DOMAIN}/v1/groups/{gid}"
    return await http_get(url)


async def get_group_roles(gid: int):
    """获取群组职位列表；失败返回空列表"""
    url = f"https://groups.{_BASE_DOMAIN}/v1/groups/{gid}/roles"
    try:
        data = await http_get(url)
        if isinstance(data, dict) and data.get("roles"):
            return data["roles"]
        if isinstance(data, dict) and data.get("data"):
            return data["data"]
        return []
    except Exception:
        return []


async def get_group_icon(gid: int):
    """获取群组图标 URL（群组图标仅支持 150x150 / 420x420）；失败返回空串"""
    url = f"https://thumbnails.{_BASE_DOMAIN}/v1/groups/icons?groupIds={gid}&size=420x420&format=Png&isCircular=false"
    try:
        data = await http_get(url)
        if data and data.get("data"):
            return data["data"][0].get("imageUrl", "")
    except Exception:
        pass
    return ""


# ============ 游戏相关 API ============

async def search_game(name: str):
    """按名称搜索游戏（关键词做 URL 编码，支持中文/空格）。

    注意：games/list 已被 Roblox 上游下线（404），当前数据源上此功能不可用，
    调用方需按“未找到/不可用”优雅降级。
    """
    encoded = urllib.parse.quote(name)
    url = f"https://games.{_BASE_DOMAIN}/v1/games/list?accessFilter=2&keyword={encoded}&limit=10&sortOrder=Relevance"
    return await http_get(url, retries=1, timeout=SEARCH_TIMEOUT)


async def get_game_info_by_universe(universe_id: int, retries: int = MAX_RETRIES):
    """按游戏ID(universeId)获取游戏详情（当前代理唯一稳定的游戏详情查询方式）"""
    url = f"https://games.{_BASE_DOMAIN}/v1/games?universeIds={universe_id}"
    return await http_get(url, retries=retries)


async def get_game_info(place_id: int, retries: int = MAX_RETRIES):
    """按地点ID获取游戏详情。

    注意：当前代理对 placeIds 参数支持不稳定（400/504），调用方应同时尝试
    get_game_info_by_universe 并取有数据的结果。
    """
    url = f"https://games.{_BASE_DOMAIN}/v1/games?placeIds={place_id}"
    return await http_get(url, retries=retries)


async def get_game_icon(game_id: int):
    """获取游戏图标 URL（参数为 universeIds，原 gameIds 会返回 400）；失败返回空串"""
    url = f"https://thumbnails.{_BASE_DOMAIN}/v1/games/icons?universeIds={game_id}&size=512x512&format=Png&isCircular=false"
    try:
        data = await http_get(url)
        if data and data.get("data"):
            return data["data"][0].get("imageUrl", "")
    except Exception:
        pass
    return ""


async def get_game_servers(place_id: int, limit: int = 10):
    """获取游戏公开服务器列表（参数为地点ID）；失败返回空列表。

    注意：servers/Public 已被 Roblox 上游禁用（403 endpoint has been disabled），
    当前数据源上恒返回空列表，保留接口以便上游恢复后直接可用。
    """
    url = f"https://games.{_BASE_DOMAIN}/v1/games/{place_id}/servers/Public?limit={limit}"
    try:
        data = await http_get(url, retries=1)
        if data:
            return data.get("data", [])
    except Exception:
        pass
    return []


# ============ 社交关系 API ============

async def _fetch_list_page(url: str, limit: int, page: int) -> list[dict]:
    """拉取列表接口并按页本地切片。

    好友等列表接口会一次性返回全部条目（limit 参数被上游忽略、无翻页 cursor），
    因此翻页在本地完成：第 page 页 = 第 (page-1)*limit 起 limit 条。
    """
    try:
        data = await http_get(url, retries=1)
    except Exception:
        return []
    if not data:
        return []
    items = data.get("data", []) or []
    start = max(0, (page - 1) * limit)
    return items[start:start + limit]


async def get_friends(uid: int, limit: int = 10, page: int = 1) -> list[dict]:
    """获取用户好友列表（第 page 页，每页 limit 个）；失败返回空列表。

    接口已不再返回用户名（name/displayName 为空串），此处自动调用
    lookup_users 批量补全名字。
    """
    url = f"https://friends.{_BASE_DOMAIN}/v1/users/{uid}/friends?limit=100"
    items = await _fetch_list_page(url, limit, page)
    if not items:
        return []
    if all(not (it.get("name") or it.get("displayName")) for it in items):
        id_map = {u["id"]: u for u in await lookup_users([it["id"] for it in items if it.get("id") is not None])}
        items = [id_map.get(it.get("id"), it) for it in items]
    return items


async def get_followers(uid: int, limit: int = 10, page: int = 1) -> list[dict]:
    """获取用户粉丝列表；失败返回空列表（上游已要求登录令牌，当前恒为空，优雅降级）"""
    url = f"https://friends.{_BASE_DOMAIN}/v1/users/{uid}/followers?limit=100"
    return await _fetch_list_page(url, limit, page)


async def get_followings(uid: int, limit: int = 10, page: int = 1) -> list[dict]:
    """获取用户关注列表；失败返回空列表（上游已要求登录令牌，当前恒为空，优雅降级）"""
    url = f"https://friends.{_BASE_DOMAIN}/v1/users/{uid}/followings?limit=100"
    return await _fetch_list_page(url, limit, page)


# ============ 图片下载 ============

async def download_image(url: str) -> bytes | None:
    """下载图片字节，失败返回 None"""
    client = get_client()
    try:
        response = await client.get(url)
        if response.status_code == 200:
            return response.content
    except Exception:
        pass
    return None
