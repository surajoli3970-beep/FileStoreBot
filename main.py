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

# Collection 1: Permanent Link Store (Ye kabhi delete nahi hoga)
files_col = db["files"]
# Collection 2: Temporary User Messages (Ye time aane par delete honge)
active_col = db["active_files"] 
config_col = db["config"]

app = Client("file_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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
    print(f"Web Server Started on Port {port}")

# --- DATABASE FUNCTIONS ---

async def get_delete_time():
    data = await config_col.find_one({"key": "del_time"})
    return data["value"] if data else 600  # Default 10 minutes

async def set_delete_time(seconds):
    await config_col.update_one(
        {"key": "del_time"}, 
        {"$set": {"value": seconds}}, 
        upsert=True
    )

# 1. File ko Permanent store karne ke liye (Channel Info)
async def add_file(unique_id, message_id):
    # Yahan delete_at hata diya, kyunki channel se delete nahi karna hai
    await files_col.insert_one({
        "unique_id": unique_id,
        "message_id": message_id
    })

async def get_file(unique_id):
    data = await files_col.find_one({"unique_id": unique_id})
    return data["message_id"] if data else None

# 2. User ko bheji gayi file track karne ke liye (Temporary Info)
async def add_active_file(user_id, message_id, delete_at):
    await active_col.insert_one({
        "user_id": user_id,
        "message_id": message_id,
        "delete_at": delete_at
    })

async def get_expired_active_files():
    now = int(time.time())
    # Sirf wo files dhundo jinka time khatam ho gaya hai
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
            channel_msg_id = await get_file(unique_id)
            
            if channel_msg_id:
                try:
                    # 1. File copy karo user ko
                    sent_msg = await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=CHANNEL_ID,
                        message_id=int(channel_msg_id),
                        caption=f"📂 **File Delivered!**\n\n⚠️ __Note: Security reasons ki wajah se ye file auto-delete ho jayegi.__\n📥 __Jaldi save/forward kar lein.__"
                    )
                    
                    # 2. Delete time calculate karo
                    del_seconds = await get_delete_time()
                    delete_at = int(time.time()) + del_seconds
                    
                    # 3. User ko warning message bhejo
                    warning_msg = await message.reply(
                        f"⏳ **Alert:** Ye file {int(del_seconds/60)} minute(s) mein delete ho jayegi.\n\n"
                        f"🛑 **Forward ya Save kar lein agar zaroorat hai.**"
                    )
                    
                    # 4. DB mein note kar lo ki is user ke message ko delete karna hai
                    # Hum file aur warning message dono ko track kar sakte hain
                    await add_active_file(message.chat.id, sent_msg.id, delete_at)
                    await add_active_file(message.chat.id, warning_msg.id, delete_at)

                except Exception as e:
                    print(e)
                    await message.reply("❌ File not found (Maybe deleted from database).")
            else:
                await message.reply("❌ Link expired or invalid.")
        except Exception as e:
            print(e)
            await message.reply("❌ Invalid Link.")
    else:
        await message.reply("👋 Welcome! Send me any file to store safely.")

@app.on_message(filters.command("settime") & filters.user(ADMIN_ID))
async def set_time_handler(client, message):
    try:
        minutes = int(message.command[1])
        seconds = minutes * 60
        await set_delete_time(seconds)
        await message.reply(f"✅ User Auto-delete time set to {minutes} minutes.")
    except:
        await message.reply("❌ Usage: `/settime 10` (for 10 minutes)")

@app.on_message((filters.document | filters.video | filters.photo | filters.audio) & filters.private)
async def file_handler(client, message):
    # Sirf Admin hi file add kar sake (Optional security)
    if message.from_user.id != ADMIN_ID:
        return 

    status_msg = await message.reply("📤 **Storing in Private Channel...**")
    try:
        # 1. Channel mein forward karo (Permanent Storage)
        forwarded = await message.forward(CHANNEL_ID)
        msg_id = forwarded.id
        
        # 2. Link Generate karo
        unique_id = f"file_{msg_id}"
        encoded_link = encode_payload(unique_id)
        bot_username = (await client.get_me()).username
        link = f"https://t.me/{bot_username}?start={encoded_link}"
        
        # 3. Database mein save karo (Bina expiry ke)
        await add_file(unique_id, msg_id)
        
        del_seconds = await get_delete_time()
        
        await status_msg.edit(
            f"✅ **File Stored Permanently!**\n\n"
            f"🔗 **Link:** {link}\n\n"
            f"ℹ️ **Info:** User ke liye ye file {int(del_seconds/60)} minutes baad auto-delete hogi, par link hamesha work karega."
        )
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")

# --- AUTO DELETE LOOP (Modified for Users) ---
async def auto_delete_loop():
    while True:
        try:
            # Expired files user ke chat se dhundo
            expired_files = await get_expired_active_files()
            
            if expired_files:
                for file_data in expired_files:
                    user_id = file_data["user_id"]
                    msg_id = file_data["message_id"]
                    
                    try:
                        # User ke chat se delete karo
                        await app.delete_messages(user_id, msg_id)
                    except Exception as e:
                        # Agar user ne bot block kiya ho ya message already delete ho
                        pass
                    
                    # DB se entry hata do taaki loop baar baar try na kare
                    await delete_active_entry(msg_id)
        except Exception as e:
            print(f"Error in Auto Delete Loop: {e}")
            
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
