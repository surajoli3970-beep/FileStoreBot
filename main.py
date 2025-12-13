import os
import asyncio
import base64
import time
from pyrogram import Client, filters, idle
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID")) 
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
MONGO_URL = os.environ.get("MONGO_URL")

# Connection to MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["filestore_bot"]
files_col = db["files"]
config_col = db["config"]

app = Client("file_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def init_db():
    # Only needed if you have specific setup logic, otherwise pass
    pass

# --- WEB SERVER (To Fix Render Port Error) ---
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
    print(f"Web Server Started on Port {port}")

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

async def add_file(unique_id, message_id, delete_at):
    await files_col.insert_one({
        "unique_id": unique_id,
        "message_id": message_id,
        "delete_at": delete_at
    })

async def get_file(unique_id):
    data = await files_col.find_one({"unique_id": unique_id})
    return data["message_id"] if data else None

async def delete_file_entry(message_id):
    await files_col.delete_one({"message_id": message_id})

async def get_expired_files():
    now = int(time.time())
    cursor = files_col.find({"delete_at": {"$lt": now}})
    return await cursor.to_list(length=100)

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
            msg_id = await get_file(unique_id)
            if msg_id:
                try:
                    await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=CHANNEL_ID,
                        message_id=int(msg_id),
                        caption=f"⚠️ This file will be auto-deleted soon."
                    )
                except Exception as e:
                    await message.reply("❌ File not found (Maybe deleted from channel).")
            else:
                await message.reply("❌ Link expired or file deleted.")
        except:
            await message.reply("❌ Invalid Link.")
    else:
        await message.reply("👋 Welcome! Send me any file to store.")

@app.on_message(filters.command("settime") & filters.user(ADMIN_ID))
async def set_time_handler(client, message):
    try:
        minutes = int(message.command[1])
        seconds = minutes * 60
        await set_delete_time(seconds)
        await message.reply(f"✅ Auto-delete time set to {minutes} minutes.")
    except:
        await message.reply("❌ Usage: `/settime 10` (for 10 minutes)")

@app.on_message((filters.document | filters.video | filters.photo | filters.audio) & filters.private)
async def file_handler(client, message):
    status_msg = await message.reply("📤 Uploading...")
    try:
        forwarded = await message.forward(CHANNEL_ID)
        msg_id = forwarded.id
        unique_id = f"file_{msg_id}"
        encoded_link = encode_payload(unique_id)
        bot_username = (await client.get_me()).username
        link = f"https://t.me/{bot_username}?start={encoded_link}"
        del_seconds = await get_delete_time()
        delete_at = int(time.time()) + del_seconds
        await add_file(unique_id, msg_id, delete_at)
        await status_msg.edit(
            f"✅ **File Stored!**\n\n"
            f"🔗 **Link:** {link}\n\n"
            f"⏳ **Expires in:** {int(del_seconds/60)} minutes."
        )
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")

# --- AUTO DELETE LOOP ---
async def auto_delete_loop():
    while True:
        try:
            expired_files = await get_expired_files()
            if expired_files:
                ids_to_delete = [f["message_id"] for f in expired_files]
                try:
                    await app.delete_messages(CHANNEL_ID, ids_to_delete)
                except: pass
                for msg_id in ids_to_delete:
                    await delete_file_entry(msg_id)
        except: pass  
        await asyncio.sleep(60)

# --- MAIN EXECUTION ---
async def main():
    await init_db()
    print("Starting Web Server & Bot...")
    await web_server()
    await app.start()
    print("Bot Started!")
    asyncio.create_task(auto_delete_loop())
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
