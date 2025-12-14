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
active_col = db["active_files"] 
config_col = db["config"]

app = Client("file_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Temporary storage for batch mode (RAM mein rahega)
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

async def add_file(unique_id, message_ids, is_batch=False):
    # message_ids ab list [] ho sakta hai
    await files_col.insert_one({
        "unique_id": unique_id,
        "message_ids": message_ids, # List of IDs stored here
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
                is_batch = file_data.get("is_batch", False)
                
                # Agar single ID hai to list bana lo (compatibility ke liye)
                if not isinstance(msg_ids, list):
                    msg_ids = [msg_ids]

                try:
                    await message.reply(f"📂 **Processing {len(msg_ids)} file(s)...**")
                    
                    del_seconds = await get_delete_time()
                    delete_at = int(time.time()) + del_seconds
                    
                    sent_files = []
                    
                    for mid in msg_ids:
                        # Copy message from channel to user
                        sent = await client.copy_message(
                            chat_id=message.chat.id,
                            from_chat_id=CHANNEL_ID,
                            message_id=int(mid),
                            caption="" if is_batch else "⚠️ __Auto-delete enabled.__"
                        )
                        sent_files.append(sent.id)
                        # Add to deletion schedule
                        await add_active_file(message.chat.id, sent.id, delete_at)
                        # FloodWait se bachne ke liye thoda wait
                        await asyncio.sleep(0.5)

                    # Warning Message
                    warning = await message.reply(
                        f"⏳ **Alert:** Ye {len(msg_ids)} files {int(del_seconds/60)} minutes mein delete ho jayengi.\n\n"
                        f"🛑 **Jaldi Save/Forward kar lein!**"
                    )
                    await add_active_file(message.chat.id, warning.id, delete_at)

                except Exception as e:
                    print(e)
                    await message.reply("❌ Error sending files. Maybe deleted from channel.")
            else:
                await message.reply("❌ Link expired or invalid.")
        except Exception as e:
            print(e)
            await message.reply("❌ Invalid Link.")
    else:
        await message.reply("👋 Welcome! Use /batch to upload multiple files.")

@app.on_message(filters.command("settime") & filters.user(ADMIN_ID))
async def set_time_handler(client, message):
    try:
        minutes = int(message.command[1])
        seconds = minutes * 60
        await set_delete_time(seconds)
        await message.reply(f"✅ User Auto-delete time set to {minutes} minutes.")
    except:
        await message.reply("❌ Usage: `/settime 10`")

# --- BATCH MODE COMMANDS ---

@app.on_message(filters.command("batch") & filters.user(ADMIN_ID))
async def batch_start(client, message):
    batch_data[message.from_user.id] = []
    await message.reply(
        "🚀 **Batch Mode Started!**\n\n"
        "Ab aap jitni chahein files (Audio, Video, Doc) bhejein.\n"
        "Jab sab upload ho jaye, tab **/done** click karein."
    )

@app.on_message(filters.command("done") & filters.user(ADMIN_ID))
async def batch_done(client, message):
    user_id = message.from_user.id
    if user_id not in batch_data or not batch_data[user_id]:
        await message.reply("❌ Aapne koi file nahi bheji! Pehle `/batch` start karein.")
        return

    msg_ids = batch_data[user_id]
    
    # Generate Link for Batch
    unique_id = f"batch_{int(time.time())}_{user_id}"
    encoded_link = encode_payload(unique_id)
    bot_username = (await client.get_me()).username
    link = f"https://t.me/{bot_username}?start={encoded_link}"
    
    # Save to DB
    await add_file(unique_id, msg_ids, is_batch=True)
    
    # Clear memory
    del batch_data[user_id]
    
    del_seconds = await get_delete_time()
    await message.reply(
        f"✅ **Batch Created Successfully!**\n"
        f"📂 Total Files: {len(msg_ids)}\n\n"
        f"🔗 **Link:** {link}\n\n"
        f"⏳ User Expiry: {int(del_seconds/60)} minutes."
    )

# --- FILE HANDLER ---

@app.on_message((filters.document | filters.video | filters.photo | filters.audio) & filters.private)
async def file_handler(client, message):
    if message.from_user.id != ADMIN_ID:
        return 

    # Check if user is in batch mode
    if message.from_user.id in batch_data:
        try:
            forwarded = await message.forward(CHANNEL_ID)
            batch_data[message.from_user.id].append(forwarded.id)
            # User ko feedback na dein taaki spam na ho, bas chupchap add karein
        except Exception as e:
            await message.reply(f"❌ Error storing file: {e}")
        return

    # Normal Single File Mode
    status_msg = await message.reply("📤 **Storing Single File...**")
    try:
        forwarded = await message.forward(CHANNEL_ID)
        msg_id = forwarded.id
        
        unique_id = f"file_{msg_id}"
        encoded_link = encode_payload(unique_id)
        bot_username = (await client.get_me()).username
        link = f"https://t.me/{bot_username}?start={encoded_link}"
        
        await add_file(unique_id, [msg_id], is_batch=False)
        
        del_seconds = await get_delete_time()
        
        await status_msg.edit(
            f"✅ **Single File Stored!**\n\n"
            f"🔗 **Link:** {link}\n"
            f"⏳ Expiry: {int(del_seconds/60)} mins."
        )
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")

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
    print("Starting...")
    await web_server()
    await app.start()
    print("Bot Started!")
    asyncio.create_task(auto_delete_loop())
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
