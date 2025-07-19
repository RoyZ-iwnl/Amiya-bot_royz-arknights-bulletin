# /pluginsDev/arkBulletinPusher/main.py

import os
import time
import asyncio
import httpx
from typing import List, Optional

# 导入 AmiyaBot 核心
from core import AmiyaBotPluginInstance, Chain, MessageBaseModel, bot as main_bot
from core.database.group import GroupSetting # 用于查询启用的群组

# 导入 Peewee 用于数据库操作
from peewee import CharField, IntegerField

# 导入 Pydantic 用于数据校验
from pydantic import BaseModel, Field

# ----------------- 插件元数据 -----------------
curr_dir = os.path.dirname(__file__)

# ----------------- Pydantic 数据模型（参考 arknights.py） -----------------
# 用于解析官方公告 API 的返回数据，确保代码健壮性

class BulletinListItem(BaseModel):
    cid: str
    title: str
    category: int
    display_time: str = Field(alias="displayTime")
    updated_at: int = Field(alias="updatedAt")

class BulletinList(BaseModel):
    list: List[BulletinListItem]

class ArkBulletinListResponse(BaseModel):
    data: BulletinList

class BulletinData(BaseModel):
    cid: str
    title: str
    content: str
    banner_image_url: str = Field(alias="bannerImageUrl")
    updated_at: int = Field(alias="updatedAt")

class ArkBulletinResponse(BaseModel):
    data: BulletinData

# ----------------- 数据库模型 -----------------
# 用于记录已经推送过的公告，防止重复发送

@main_bot.db.table
class BulletinRecord(MessageBaseModel):
    cid: str = CharField(unique=True) # 公告的唯一ID
    record_time: int = IntegerField() # 记录时间

# ----------------- 插件实例与生命周期 -----------------

class ArknightsBulletinPluginInstance(AmiyaBotPluginInstance):
    def install(self):
        """插件安装时执行，创建数据库表"""
        BulletinRecord.create_table(safe=True)

bot = ArknightsBulletinPluginInstance(
    name='制作组通讯推送',
    version='1.0.0',
    plugin_id='royz-arknights-bulletin',
    plugin_type="official",
    description='定时获取明日方舟制作组通讯并以图片形式推送到群',
    document=f'{curr_dir}/README.md',
    instruction=f'{curr_dir}/README.md',
    global_config_schema=f'{curr_dir}/config_schema.json',
    global_config_default=f'{curr_dir}/config_default.yaml',
)

# ----------------- 核心功能：定时任务 -----------------
# 参考 weibo.py 的定时任务实现
@bot.timed_task(each=bot.get_config('checkInterval', 300))
async def timed_check_bulletin(_):
    # 如果未启用，则不执行
    if not bot.get_config('enablePush'):
        return

    # 获取需要推送的关键词
    keywords_to_check: List[str] = bot.get_config('keywords')
    if not keywords_to_check:
        return

    # 使用 httpx 异步请求
    async with httpx.AsyncClient() as client:
        try:
            # 1. 获取公告列表
            list_api = "https://ak-webview.hypergryph.com/api/game/bulletinList?target=IOS"
            resp = await client.get(list_api, timeout=10)
            resp.raise_for_status()
            bulletin_list_data = ArkBulletinListResponse.model_validate(resp.json())
        except Exception as e:
            main_bot.logger.error(f"获取方舟公告列表失败: {e}")
            return

        # 2. 遍历公告，寻找新公告
        for bulletin in sorted(bulletin_list_data.data.list, key=lambda x: x.updated_at, reverse=True):
            # 检查标题是否包含任一关键词
            if not any(keyword in bulletin.title for keyword in keywords_to_check):
                continue
            
            # 检查是否已推送
            if BulletinRecord.get_or_none(cid=bulletin.cid):
                continue

            # 找到了新的、匹配的公告，开始处理
            main_bot.logger.info(f"发现新的制作组通讯: {bulletin.title} ({bulletin.cid})")

            try:
                # 3. 获取公告详情
                detail_api = f"https://ak-webview.hypergryph.com/api/game/bulletin/{bulletin.cid}"
                detail_resp = await client.get(detail_api, timeout=10)
                detail_resp.raise_for_status()
                detail_data = ArkBulletinResponse.model_validate(detail_resp.json()).data
            except Exception as e:
                main_bot.logger.error(f"获取公告 {bulletin.cid} 详情失败: {e}")
                continue

            # 4. 渲染图片（参考 imgbot/user.py）
            template_path = f'{curr_dir}/template/bulletin.html'
            render_data = {
                'title': detail_data.title,
                'banner_url': detail_data.banner_image_url,
                'content': detail_data.content, # content 本身是 HTML
                'publish_time': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(detail_data.updated_at))
            }
            
            try:
                # 使用 Chain().html() 生成图片，它会返回一个包含图片的 Chain 对象
                image_chain = Chain().html(template_path, render_data, width=800, height=1)
            except Exception as e:
                main_bot.logger.error(f"渲染公告图片失败: {e}")
                continue

            # 5. 推送到群组
            # 查找所有开启了此功能的频道
            target_channels = GroupSetting.select().where(
                (GroupSetting.function_id == bot.plugin_id) &
                (GroupSetting.status == 1)
            )

            if not target_channels:
                main_bot.logger.info("没有找到需要推送的群组。")

            push_tasks = []
            for item in target_channels:
                instance = main_bot[item.bot_id]
                if instance:
                    # 使用 asyncio.create_task 实现异步并发推送
                    task = asyncio.create_task(
                        instance.send_message(image_chain, channel_id=item.group_id)
                    )
                    push_tasks.append(task)
            
            if push_tasks:
                await asyncio.wait(push_tasks)
                main_bot.logger.info(f"已向 {len(push_tasks)} 个群组推送公告 {bulletin.cid}")

            # 6. 记录已推送的公告ID
            BulletinRecord.create(cid=bulletin.cid, record_time=int(time.time()))

            # 避免一次性推送过多，只处理最新的一个
            break
