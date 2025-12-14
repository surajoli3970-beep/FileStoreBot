import os
import asyncio
import base64
import time
import logging
import sys
from pyrogram import Client, filters, idle
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION CHECK ---
try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    CHANNEL_ID = int(os.environ.get("CHANNEL_ID")) 
    ADMIN_ID = int(os.environ.get("ADMIN_ID"))
    MONGO_URL = os.environ.get("MONGO_URL")
    
    if not all([API_ID, API_HASH, BOT_TOKEN, CHANNEL_ID, ADMIN_ID, MONGO_URL]):
        raise ValueError("Kuch Environment Variables Missing hain!")
except Exception as e:
    logger.error(f"Configuration Error: {e}")
    sys.exit(1)

# Connection to MongoDB
try:
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    db = mongo_client["filestore_bot"]
    files_col = db["files"]
    active_col = db["active_files"] 
    config_col = db["config"]
    logger.info("MongoDB Connected Successfully!")
except Exception as e:
    logger.error(f"MongoDB Connection Failed: {e}")
    sys.exit(1)

app = Client("file_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Temporary storage for batch mode
batch_data = {}

async def init_db():
    pass

# --- WEB SERVER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is Running Successfully!")

    web_app = web.Application()
    web_app.add_routes([web.get('/', handle)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web Server Started on Port {port}")

# --- DATABASE FUNCTIONS ---

async def get_delete_time():
    data = await config_col.find_one({"key": "del_time"})
    return data["value"] if data else 600 

async def set_delete_time(seconds):
    await config_col.update_one(
        {"key": "del_time"}, 
        {"$set": {"value": seconds}}, 
        upsert=True
    )

# New: Custom Message Fetch
async def get_alert_msg():
    data = await config_col.find_one({"key": "alert_msg"})
    # Default Message agar set nahi kiya hai
    default_msg = "⏳ **Alert:** Ye file {time} minutes mein delete ho jayegi.\n\n🛑 **Jaldi Forward ya Save kar lein!**"
    return data["value"] if data else default_msg

# New: Custom Message Set
async def set_alert_msg(msg):
    await config_col.update_one(
        {"key": "alert_msg"}, 
        {"$set": {"value": msg}}, 
        upsert=True
    )

async def add_file(unique_id, message_ids, is_batch=False):
    await files_col.insert_one({
        "unique_id": unique_id,
        "message_ids": message_ids,
        "is_batch": is_batch
    })

async def get_file(unique_id):
    return await files_col.find_one({"unique_id": unique_id})

async def add_active_file(user_id, message_id, delete_at):
    await active_col.insert_one({
        "user_id": user_id,
        "message_id": message_id,
        "delete_at": delete_at
    })

async def get_expired_active_files():
    now = int(time.time())
    cursor = active_col.find({"delete_at": {"$lt": now}})
    return await cursor.to_list(length=100)

async def delete_active_entry(message_id):
    await active_col.delete_one({"message_id": message_id})

# --- HELPER FUNCTIONS ---
def encode_payload(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

def decode_payload(payload: str) -> str:
    padding = "=" * (4 - len(payload) % 4)
    return base64.urlsafe_b64decode(payload + padding).decode()

# --- BOT COMMANDS ---

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    if len(message.command) > 1:
        payload = message.command[1]
        try:
            unique_id = decode_payload(payload)
            file_data = await get_file(unique_id)
            
            if file_data:
                msg_ids = file_data.get("message_ids")
                if not isinstance(msg_ids, list):
                    msg_ids = [msg_ids]

                try:
                    loading_msg = await message.reply(f"📂 **Processing {len(msg_ids)} file(s)...**")
                    del_seconds = await get_delete_time()
                    delete_at = int(time.time()) + del_seconds
                    
                    # Custom Message Tayar karo
                    raw_alert = await get_alert_msg()
                    formatted_alert = raw_alert.replace("{time}", str(int(del_seconds/60)))

                    for mid in msg_ids:
                        sent = await client.copy_message(
                            chat_id=message.chat.id,
                            from_chat_id=CHANNEL_ID,
                            message_id=int(mid)
                        )
                        await add_active_file(message.chat.id, sent.id, delete_at)
                        await asyncio.sleep(0.5)

                    await loading_msg.delete()
                    
                    # Send Custom Alert Message
                    warning = await message.reply(formatted_alert)
                    await add_active_file(message.chat.id, warning.id, delete_at)

                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    await message.reply("❌ Error sending files. Maybe deleted from channel.")
            else:
                await message.reply("❌ Link expired or invalid.")
        except Exception as e:
            logger.error(f"Link Error: {e}")
            await message.reply("❌ Invalid Link.")
    else:
        await message.reply("👋 Welcome! Send multiple files to get links instantly.")

# --- SETTINGS COMMANDS ---

@app.on_message(filters.command("settime") & filters.user(ADMIN_ID))
async def set_time_handler(client, message):
    try:
        minutes = int(message.command[1])
        seconds = minutes * 60
        await set_delete_time(seconds)
        await message.reply(f"✅ User Auto-delete time set to {minutes} minutes.")
    except:
        await message.reply("❌ Usage: `/settime 10`")

# NEW COMMAND: Custom Message Set Karne ke liye
@app.on_message(filters.command("setalert") & filters.user(ADMIN_ID))
async def set_alert_handler(client, message):
    # Check if message has text
    if len(message.command) < 2:
        await message.reply(
            "❌ **Message likhna bhool gaye!**\n\n"
            "Usage Example:\n"
            "`/setalert Ye file {time} minutes mein delete hogi.`\n\n"
            "Note: `{time}` likhna zaroori hai, wahan automatic minutes aa jayenge."
        )
        return

    # Command ke baad ka saara text utha lo
    new_msg = message.text.split(None, 1)[1]
    
    await set_alert_msg(new_msg)
    await message.reply(f"✅ **New Alert Message Saved!**\n\nPreview:\n{new_msg}")


# --- BATCH MODE (One Link for Many Files) ---

@app.on_message(filters.command("batch") & filters.user(ADMIN_ID))
async def batch_start(client, message):
    batch_data[message.from_user.id] = []
    await message.reply("🚀 **Batch Mode Started!**\nSend files. Type **/done** for a single link.")

@app.on_message(filters.command("done") & filters.user(ADMIN_ID))
async def batch_done(client, message):
    user_id = message.from_user.id
    if user_id not in batch_data or not batch_data[user_id]:
        await message.reply("❌ List empty!")
        return

    msg_ids = batch_data[user_id]
    unique_id = f"batch_{int(time.time())}_{user_id}"
    encoded_link = encode_payload(unique_id)
    bot_username = (await client.get_me()).username
    link = f"https://t.me/{bot_username}?start={encoded_link}"
    
    await add_file(unique_id, msg_ids, is_batch=True)
    del batch_data[user_id]
    
    del_seconds = await get_delete_time()
    await message.reply(f"✅ **Batch Created!**\n🔗 **Link:** {link}\n⏳ Expiry: {int(del_seconds/60)} mins.")

# --- CONTENT HANDLER (SINGLE LINKS PER FILE) ---
@app.on_message(
    (filters.document | filters.video | filters.photo | filters.audio | filters.text) 
    & filters.private
)
async def content_handler(client, message):
    if message.command: return # Ignore commands

    if message.from_user.id != ADMIN_ID: return 

    # Agar Batch Mode ON hai to wahan collect karo
    if message.from_user.id in batch_data:
        try:
            forwarded = await message.forward(CHANNEL_ID)
            batch_data[message.from_user.id].append(forwarded.id)
        except Exception as e:
            await message.reply(f"❌ Error: {e}")
        return

    # Normal Mode: Har file ka ALAG Link banega
    # User ko 'reply' nahi karte warna flood ho jayega agar 20 file bheji to.
    # Hum bas Link bhejenge.
    try:
        forwarded = await message.forward(CHANNEL_ID)
        msg_id = forwarded.id
        unique_id = f"file_{msg_id}"
        encoded_link = encode_payload(unique_id)
        bot_username = (await client.get_me()).username
        link = f"https://t.me/{bot_username}?start={encoded_link}"
        
        await add_file(unique_id, [msg_id], is_batch=False)
        
        # Simple response taaki user 20 file bheje to spam na lage
        await message.reply(f"🔗 {link}", quote=True)
        
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# --- AUTO DELETE LOOP ---
async def auto_delete_loop():
    while True:
        try:
            expired_files = await get_expired_active_files()
            if expired_files:
                for file_data in expired_files:
                    try:
                        await app.delete_messages(file_data["user_id"], file_data["message_id"])
                    except: pass
                    await delete_active_entry(file_data["message_id"])
        except: pass    
        await asyncio.sleep(60)

# --- MAIN ---
async def main():
    await init_db()
    logger.info("Starting Web Server & Bot...")
    await web_server()
    await app.start()
    logger.info("Bot Started Successfully!")
    asyncio.create_task(auto_delete_loop())
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
