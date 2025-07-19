import os
import time
import asyncio
import base64
import aiohttp
from typing import List, Tuple, Optional, Dict, Any

from core import Message, Chain, log, AmiyaBotPluginInstance, bot as main_bot
from core.database.group import GroupSetting
from amiyabot.network.httpRequests import http_requests
from core.database.messages import MessageBaseModel, table
from peewee import CharField, IntegerField
from pydantic import BaseModel, Field

# ----------------- 插件元数据 -----------------
curr_dir = os.path.dirname(__file__)

# JSON文件路径
PUSH_GROUPS_FILE = os.path.join(curr_dir, 'push_groups.json')

# ----------------- JSON文件管理 -----------------
def load_push_groups() -> Dict[str, Any]:
    """加载推送群组配置"""
    if os.path.exists(PUSH_GROUPS_FILE):
        try:
            with open(PUSH_GROUPS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log.error(f"读取推送群组配置失败: {e}")
            return {}
    return {}

def save_push_groups(data: Dict[str, Any]) -> bool:
    """保存推送群组配置"""
    try:
        with open(PUSH_GROUPS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"保存推送群组配置失败: {e}")
        return False

def add_push_group(channel_id: str, bot_id: str) -> bool:
    """添加推送群组"""
    data = load_push_groups()
    group_key = f"{channel_id}_{bot_id}"
    data[group_key] = {
        'channel_id': channel_id,
        'bot_id': bot_id,
        'enabled': True,
        'added_time': int(time.time())
    }
    return save_push_groups(data)

def remove_push_group(channel_id: str, bot_id: str) -> bool:
    """移除推送群组"""
    data = load_push_groups()
    group_key = f"{channel_id}_{bot_id}"
    if group_key in data:
        data[group_key]['enabled'] = False
        return save_push_groups(data)
    return True

def get_enabled_groups() -> List[Dict[str, str]]:
    """获取所有启用推送的群组"""
    data = load_push_groups()
    enabled_groups = []
    for group_key, group_info in data.items():
        if group_info.get('enabled', False):
            enabled_groups.append({
                'channel_id': group_info['channel_id'],
                'bot_id': group_info['bot_id']
            })
    return enabled_groups

# ----------------- Pydantic 数据模型 -----------------
class BulletinListItem(BaseModel):
    cid: str
    title: str
    category: int
    display_time: str = Field(alias="displayTime")
    updated_at: int = Field(alias="updatedAt")

class BulletinList(BaseModel):
    list: List[BulletinListItem]

class ArkBulletinListResponse(BaseModel):
    class Config:
        arbitrary_types_allowed = True
        
    data: BulletinList

class BulletinData(BaseModel):
    cid: str
    title: str
    content: str
    banner_image_url: str = Field(alias="bannerImageUrl")
    updated_at: int = Field(alias="updatedAt")

class ArkBulletinResponse(BaseModel):
    class Config:
        arbitrary_types_allowed = True

    data: BulletinData

# ----------------- 数据库模型 -----------------
@table
class BulletinRecord(MessageBaseModel):
    cid: str = CharField(unique=True)
    record_time: int = IntegerField()

# ----------------- 插件实例与生命周期 -----------------
class ArknightsBulletinPluginInstance(AmiyaBotPluginInstance):
    def install(self):
        BulletinRecord.create_table(safe=True)

bot = ArknightsBulletinPluginInstance(
    name='制作组通讯推送',
    version='1.1.0',
    plugin_id='royz-arknights-bulletin',
    plugin_type="",
    description='定时或手动获取明日方舟制作组通讯',
    document=f'{curr_dir}/README.md',
    instruction=f'{curr_dir}/README.md',
    global_config_schema=f'{curr_dir}/config_schema.json',
    global_config_default=f'{curr_dir}/config_default.yaml',
)

# ----------------- 状态变量 -----------------
last_check_timestamp = 0.0

# ----------------- 核心逻辑 -----------------
async def get_latest_bulletin(force_latest: bool = False, message: Optional[Message] = None) -> Optional[Tuple[Chain, str]]:
    """
    获取最新的制作组通讯。

    1.  模拟游戏客户端的请求头 (headers)，包括 User-Agent 和 X-Unity-Version，
        以防止被服务器拒绝访问。
    2.  将这个 headers 应用于所有的网络请求，包括公告列表、详情和横幅图片。
    """
    keywords_to_check: List[str] = bot.get_config('keywords')
    if not keywords_to_check:
        return None

    # 模拟明日方舟客户端的请求头，防止被服务器屏蔽
    headers = {
        'User-Agent': 'arknights/40 CFNetwork/1329 Darwin/21.3.0',
        'X-Unity-Version': '2021.3.39f1',
        'Accept': '*/*',
        'Accept-Encoding': 'gzip',
        'Connection': 'keep-alive'
    }

    try:
        list_api = "https://ak-webview.hypergryph.com/api/game/bulletinList?target=IOS"
        # 使用新的请求头进行请求
        resp = await http_requests.get(list_api, timeout=10, headers=headers)
        if not resp or resp.response.status != 200:
            log.error(f"获取官方公告列表失败，状态码: {resp.response.status if resp else '无响应'}")
            return None
        bulletin_list_data = ArkBulletinListResponse.parse_obj(resp.json)
    except Exception as e:
        log.error(f"解析官方公告列表时发生异常: {e}")
        return None

    for bulletin in sorted(bulletin_list_data.data.list, key=lambda x: x.updated_at, reverse=True):
        if not any(keyword in bulletin.title for keyword in keywords_to_check):
            continue
        
        if not force_latest and BulletinRecord.get_or_none(cid=bulletin.cid):
            continue

        log.info(f"发现目标通讯: {bulletin.title} ({bulletin.cid})")

        try:
            detail_api = f"https://ak-webview.hypergryph.com/api/game/bulletin/{bulletin.cid}"
            # 同样使用请求头
            detail_resp = await http_requests.get(detail_api, timeout=10, headers=headers)
            if not detail_resp or detail_resp.response.status != 200:
                log.error(f"获取公告 {bulletin.cid} 详情失败")
                continue
            detail_data = ArkBulletinResponse.parse_obj(detail_resp.json).data

            banner_base64_string = ""
            try:
                # 直接使用 aiohttp 下载图片，以获取原始二进制数据
                async with aiohttp.ClientSession() as session:
                    async with session.get(detail_data.banner_image_url, headers=headers, timeout=15) as banner_resp:
                        # aiohttp 使用 .status 判断状态码
                        if banner_resp.status == 200:
                            # 使用 .read() 获取二进制内容
                            image_bytes = await banner_resp.read()
                            content_type = banner_resp.headers.get('Content-Type', 'image/png')
                            
                            # 对二进制内容进行Base64编码
                            encoded_data = base64.b64encode(image_bytes).decode('utf-8')
                            banner_base64_string = f"data:{content_type};base64,{encoded_data}"
                        else:
                            log.warning(f"下载横幅图片失败，URL: {detail_data.banner_image_url}, 状态码: {banner_resp.status}")
            except Exception as img_e:
                log.error(f"下载或转换横幅图片时出错: {img_e}", exc_info=True)

            # 将标题中的 \n 和 \\n 替换为空格
            processed_title = detail_data.title.replace('\\n', ' ').replace('\n', ' ')

            template_path = f'{curr_dir}/template/bulletin.html'
            render_data = {
                'title': processed_title,
                'banner_url': banner_base64_string,
                'content': detail_data.content,
                'publish_time': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(detail_data.updated_at))
            }
            
            chain_builder = Chain(message) if message else Chain()
            image_chain = chain_builder.html(template_path, render_data, width=800, height=1)
            
            return image_chain, detail_data.cid
        
        except Exception as e:
            log.error(f"处理公告 {bulletin.cid} 详情时发生异常: {e}", exc_info=True) 
            continue
    
    return None

# ----------------- 推送开关指令 -----------------
@bot.on_message(keywords=['开启制作组通讯推送'], level=5)
async def enable_push(data: Message):
    if not data.is_admin:
        return Chain(data).text('抱歉博士，只有管理员才能设置制作组通讯推送哦。')

    # 检查是否已经开启
    enabled_groups = get_enabled_groups()
    for group in enabled_groups:
        if group['channel_id'] == data.channel_id and group['bot_id'] == data.instance.appid:
            return Chain(data).text('博士，本群已经开启制作组通讯推送了，请勿重复操作。')

    # 添加到推送列表
    if add_push_group(data.channel_id, data.instance.appid):
        log.info(f"群组 {data.channel_id} 开启了制作组通讯推送")
        return Chain(data).text('指令确认，本群的制作组通讯推送功能已开启！')
    else:
        return Chain(data).text('开启推送功能时出现错误，请稍后重试。')

@bot.on_message(keywords=['关闭制作组通讯推送'], level=5)
async def disable_push(data: Message):
    if not data.is_admin:
        return Chain(data).text('抱歉博士，只有管理员才能设置制作组通讯推送哦。')

    # 从推送列表中移除
    if remove_push_group(data.channel_id, data.instance.appid):
        log.info(f"群组 {data.channel_id} 关闭了制作组通讯推送")
        return Chain(data).text('指令确认，本群已关闭制作组通讯推送功能。')
    else:
        return Chain(data).text('关闭推送功能时出现错误，请稍后重试。')

# ----------------- 手动触发指令 -----------------
@bot.on_message(keywords=['测试通讯推送'], level=5)
async def manual_check(data: Message):
    await data.send(Chain(data).text('正在检查最新的制作组通讯，请稍候...'))
    
    result = await get_latest_bulletin(force_latest=True, message=data)
    
    if result:
        image_chain, _ = result
        await data.send(image_chain)
    else:
        await data.send(Chain(data).text('博士，目前没有找到符合条件的最新制作组通讯。'))

# ----------------- 定时任务核心执行逻辑 -----------------
async def execute_bulletin_push():
    result = await get_latest_bulletin(force_latest=False, message=None)
    
    if not result:
        return

    image_chain, bulletin_cid = result

    if BulletinRecord.get_or_none(cid=bulletin_cid):
        return

    # 获取所有启用了推送的群组
    target_groups = get_enabled_groups()

    if not target_groups:
        log.info("发现新通讯，但没有找到需要推送的群组。")
        # 即使没有群推送，也应记录下来防止下次重复检查
        BulletinRecord.create(cid=bulletin_cid, record_time=int(time.time()))
        return

    push_tasks = []
    for group in target_groups:
        instance = main_bot[group['bot_id']]
        if instance:
            task = asyncio.create_task(
                instance.send_message(image_chain, channel_id=group['channel_id'])
            )
            push_tasks.append(task)
    
    if push_tasks:
        await asyncio.wait(push_tasks)
        log.info(f"已向 {len(push_tasks)} 个群组推送公告 {bulletin_cid}")

    BulletinRecord.create(cid=bulletin_cid, record_time=int(time.time()))

# ----------------- 定时任务调度器 -----------------
@bot.timed_task(each=60)
async def timed_check_scheduler(_):
    global last_check_timestamp

    if not bot.get_config('enablePush'):
        return

    try:
        interval_seconds = int(bot.get_config('checkInterval', 120))
    except (ValueError, TypeError):
        log.warning(f"配置中的 'checkInterval' 值无效，将使用默认值120秒。")
        interval_seconds = 120
    
    current_time = time.time()
    if current_time - last_check_timestamp >= interval_seconds:
        log.info(f"到达预定检查时间（间隔: {interval_seconds} 秒），准备执行通讯检查。")
        last_check_timestamp = current_time
        await execute_bulletin_push()
