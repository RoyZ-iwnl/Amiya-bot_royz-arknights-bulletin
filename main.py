import os
import time
import asyncio
import base64
from typing import List, Tuple, Optional

from core import Message, Chain, log, AmiyaBotPluginInstance, bot as main_bot
from core.database.group import GroupSetting
from amiyabot.network.httpRequests import http_requests
from core.database.messages import MessageBaseModel, table
from peewee import CharField, IntegerField
from pydantic import BaseModel, Field

# ----------------- 插件元数据 -----------------
curr_dir = os.path.dirname(__file__)

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
    version='1.0.0',
    plugin_id='royz-arknights-bulletin',
    plugin_type="",
    description='定时或手动获取明日方舟制作组通讯',
    document=f'{curr_dir}/README.md',
    instruction=f'{curr_dir}/README.md',
    global_config_schema=f'{curr_dir}/config_schema.json',
    global_config_default=f'{curr_dir}/config_default.yaml',
)

# ----------------- 核心逻辑 -----------------
async def get_latest_bulletin(force_latest: bool = False, message: Optional[Message] = None) -> Optional[Tuple[Chain, str]]:
    keywords_to_check: List[str] = bot.get_config('keywords')
    if not keywords_to_check:
        return None

    try:
        list_api = "https://ak-webview.hypergryph.com/api/game/bulletinList?target=IOS"
        resp = await http_requests.get(list_api, timeout=10)
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
            detail_resp = await http_requests.get(detail_api, timeout=10)
            if not detail_resp or detail_resp.response.status != 200:
                log.error(f"获取公告 {bulletin.cid} 详情失败")
                continue
            detail_data = ArkBulletinResponse.parse_obj(detail_resp.json).data

            
            # 下载横幅图片并转换为 Base64
            banner_base64_string = ""
            try:
                banner_resp = await http_requests.get(detail_data.banner_image_url)
                if banner_resp and banner_resp.status_code == 200:
                    # 获取正确的图片 MIME 类型，如 image/png, image/jpeg
                    content_type = banner_resp.headers.get('Content-Type', 'image/png')
                    # 对图片二进制数据进行 Base64 编码
                    encoded_data = base64.b64encode(banner_resp.content).decode('utf-8')
                    # 构造成 Data URI
                    banner_base64_string = f"data:{content_type};base64,{encoded_data}"
                else:
                    log.warning(f"下载横幅图片失败: {detail_data.banner_image_url}")
            except Exception as img_e:
                log.error(f"转换横幅图片为Base64时出错: {img_e}")

            # 2. 处理标题中的换行符
            processed_title = detail_data.title.replace('\\n', '<br>').replace('\n', '<br>')

            # 3. 准备最终的数据字典
            template_path = f'{curr_dir}/template/bulletin.html'
            render_data = {
                'title': processed_title,
                'banner_url': banner_base64_string,  # 使用 Base64 字符串
                'content': detail_data.content,
                'publish_time': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(detail_data.updated_at))
            }
            
            # 4. 创建消息链并渲染
            chain_builder = Chain(message) if message else Chain()
            image_chain = chain_builder.html(template_path, render_data, width=800, height=1)
            
            return image_chain, detail_data.cid
        
        except Exception as e:
            log.error(f"处理公告 {bulletin.cid} 详情时发生异常: {e}", exc_info=True) 
            continue
    
    return None

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


# ----------------- 定时任务 -----------------
@bot.timed_task(each=bot.get_config('checkInterval', 300))
async def timed_check_bulletin(_):
    if not bot.get_config('enablePush'):
        return

    result = await get_latest_bulletin(force_latest=False, message=None)
    
    if not result:
        return

    image_chain, bulletin_cid = result

    if BulletinRecord.get_or_none(cid=bulletin_cid):
        return

    target_channels = GroupSetting.select().where(
        (GroupSetting.function_id == bot.plugin_id) &
        (GroupSetting.status == 1)
    )

    if not target_channels:
        log.info("没有找到需要推送的群组。")
        return

    push_tasks = []
    for item in target_channels:
        instance = main_bot[item.bot_id]
        if instance:
            task = asyncio.create_task(
                instance.send_message(image_chain, channel_id=item.group_id)
            )
            push_tasks.append(task)
    
    if push_tasks:
        await asyncio.wait(push_tasks)
        log.info(f"已向 {len(push_tasks)} 个群组推送公告 {bulletin_cid}")

    BulletinRecord.create(cid=bulletin_cid, record_time=int(time.time()))
