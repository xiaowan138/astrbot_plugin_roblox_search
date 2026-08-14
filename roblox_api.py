"""
roblox_api.py —— Roblox 查询 API 封装（迁移自 nonebot_plugin_roblox_search/http_utils.py）

改动说明：
- 原插件使用同步 `requests` 库，在异步协程中调用会阻塞事件循环；
  此处按 AstrBot 官方建议改用 `httpx.AsyncClient`，并复用原插件的
  请求头、30s 超时、3 次重试、429 限流退避逻辑。
- 数据源与原插件一致，均为第三方代理 API（*.rotunnel.com），非 Roblox 官方接口。
"""

import asyncio

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.roblox.com",
    "Referer": "https://www.roblox.com/",
}

TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 2

_client: httpx.AsyncClient | None = None


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


async def http_get(url: str, retries: int = MAX_RETRIES):
    """GET 请求，带 429 限流退避与失败重试；最终失败返回空 dict"""
    client = get_client()
    for attempt in range(retries):
        try:
            response = await client.get(url)
            if response.status_code == 429:
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    print(f"[HTTP] 429 Too Many Requests, 第{attempt + 1}次重试，等待{wait_time}秒...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    print(f"[HTTP GET Error] {url}: 429 Too Many Requests (已重试{retries}次)")
                    return {}
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if attempt < retries - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print(f"[HTTP] 请求失败，第{attempt + 1}次重试: {str(e)}，等待{wait_time}秒...")
                await asyncio.sleep(wait_time)
                continue
            else:
                print(f"[HTTP GET Error] {url}: {str(e)}")
                return {}


async def http_post(url: str, data=None, retries: int = MAX_RETRIES):
    """POST 请求，带 429 限流退避与失败重试；最终失败返回空 dict"""
    client = get_client()
    for attempt in range(retries):
        try:
            response = await client.post(url, json=data)
            if response.status_code == 429:
                if attempt < retries - 1:
                    wait_time = RETRY_DELAY * (attempt + 1)
                    print(f"[HTTP] 429 Too Many Requests, 第{attempt + 1}次重试，等待{wait_time}秒...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    print(f"[HTTP POST Error] {url}: 429 Too Many Requests (已重试{retries}次)")
                    return {}
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if attempt < retries - 1:
                wait_time = RETRY_DELAY * (attempt + 1)
                print(f"[HTTP] 请求失败，第{attempt + 1}次重试: {str(e)}，等待{wait_time}秒...")
                await asyncio.sleep(wait_time)
                continue
            else:
                print(f"[HTTP POST Error] {url}: {str(e)}")
                return {}


# ============ 用户相关 API ============

async def search_user(username: str):
    """通过用户名搜索用户，返回第一个匹配的用户 dict 或 None"""
    import urllib.parse
    encoded_username = urllib.parse.quote(username)
    url = f"https://users.rotunnel.com/v1/users/search?keyword={encoded_username}"
    try:
        data = await http_get(url)
        if data.get("data"):
            return data["data"][0]
    except Exception:
        pass
    return None


async def get_user_details(user_id: int):
    """获取用户完整资料"""
    url = f"https://users.rotunnel.com/v1/users/{user_id}"
    try:
        return await http_get(url)
    except Exception:
        return None


async def get_user_presence(user_id: int):
    """获取用户在线状态"""
    url = "https://presence.rotunnel.com/v1/presence/users"
    try:
        data = await http_post(url, data={"userIds": [user_id]})
        if data.get("userPresences"):
            return data["userPresences"][0]
    except Exception:
        pass
    return None


async def get_user_groups(user_id: int, limit: int = 5):
    """获取用户加入的群组（前 limit 个）"""
    url = f"https://groups.rotunnel.com/v1/users/{user_id}/groups/roles?limit={limit}"
    try:
        data = await http_get(url)
        return data.get("data", [])
    except Exception:
        return []


async def get_friend_count(user_id: int):
    """获取用户好友数"""
    url = f"https://friends.rotunnel.com/v1/users/{user_id}/friends/count"
    try:
        data = await http_get(url)
        return data.get("count", 0)
    except Exception:
        return 0


async def get_follower_count(user_id: int):
    """获取用户粉丝数"""
    url = f"https://friends.rotunnel.com/v1/users/{user_id}/followers/count"
    try:
        data = await http_get(url)
        return data.get("count", 0)
    except Exception:
        return 0


async def get_following_count(user_id: int):
    """获取用户关注数"""
    url = f"https://friends.rotunnel.com/v1/users/{user_id}/followings/count"
    try:
        data = await http_get(url)
        return data.get("count", 0)
    except Exception:
        return 0


async def get_avatar_url(user_id: int):
    """获取用户 3D 形象图 URL"""
    url = f"https://thumbnails.rotunnel.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png"
    try:
        data = await http_get(url)
        if data.get("data") and len(data["data"]) > 0:
            return data["data"][0].get("imageUrl", "")
    except Exception:
        pass
    return ""


async def get_headshot_url(user_id: int):
    """获取用户头像框 URL"""
    url = f"https://thumbnails.rotunnel.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=false"
    try:
        data = await http_get(url)
        if data.get("data") and len(data["data"]) > 0:
            return data["data"][0].get("imageUrl", "")
    except Exception:
        pass
    return ""


# ============ 群组相关 API ============

async def search_group(name: str):
    """按名称模糊搜索群组"""
    url = f"https://groups.rotunnel.com/v1/groups/search?keyword={name}&limit=10"
    return await http_get(url)


async def get_group_info(gid: int):
    """获取群组基本信息"""
    url = f"https://groups.rotunnel.com/v1/groups/{gid}"
    return await http_get(url)


async def get_group_roles(gid: int):
    """获取群组职位列表"""
    url = f"https://groups.rotunnel.com/v1/groups/{gid}/roles"
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
    """获取群组图标 URL"""
    url = f"https://thumbnails.rotunnel.com/v1/groups/icons?groupIds={gid}&size=512x512&format=Png&isCircular=false"
    try:
        data = await http_get(url)
        return data.get("data", [{}])[0].get("imageUrl", "")
    except Exception:
        return ""


# ============ 游戏相关 API ============

async def search_game(name: str):
    """按名称搜索游戏"""
    url = f"https://games.rotunnel.com/v1/games/list?accessFilter=2&keyword={name}&limit=10&sortOrder=Relevance"
    return await http_get(url)


async def get_game_info(place_id: int):
    """按地点ID获取游戏详情"""
    url = f"https://games.rotunnel.com/v1/games?placeIds={place_id}"
    return await http_get(url)


async def get_game_icon(game_id: int):
    """获取游戏图标 URL"""
    url = f"https://thumbnails.rotunnel.com/v1/games/icons?gameIds={game_id}&size=512x512&format=Png&isCircular=false"
    try:
        data = await http_get(url)
        return data.get("data", [{}])[0].get("imageUrl", "")
    except Exception:
        return ""


async def get_game_servers(game_id: int, limit: int = 5):
    """获取游戏公开服务器列表"""
    url = f"https://games.rotunnel.com/v1/games/{game_id}/servers/Public?limit={limit}"
    try:
        data = await http_get(url)
        return data.get("data", [])
    except Exception:
        return []


# ============ 社交关系 API ============

async def get_friends(uid: int, limit: int = 10):
    """获取用户好友列表"""
    url = f"https://friends.rotunnel.com/v1/users/{uid}/friends?limit={limit}"
    try:
        data = await http_get(url)
        return data.get("data", [])
    except Exception:
        return []


async def get_followers(uid: int, limit: int = 10):
    """获取用户粉丝列表"""
    url = f"https://friends.rotunnel.com/v1/users/{uid}/followers?limit={limit}"
    try:
        data = await http_get(url)
        return data.get("data", [])
    except Exception:
        return []


async def get_followings(uid: int, limit: int = 10):
    """获取用户关注列表"""
    url = f"https://friends.rotunnel.com/v1/users/{uid}/followings?limit={limit}"
    try:
        data = await http_get(url)
        return data.get("data", [])
    except Exception:
        return []


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
