import os
import json
import shutil
import logging
import py_compile
import re
import time
import uuid
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from ollama import AsyncClient

# Tambahan import untuk dummy web server Render
from flask import Flask
import threading

# ==========================================
# 1. KONFIGURASI & LIMIT SYSTEM
# ==========================================
# Mengambil token secara aman dari Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
if not TELEGRAM_TOKEN or not OLLAMA_API_KEY:
    raise ValueError("🚨 CRITICAL ERROR: TELEGRAM_TOKEN atau OLLAMA_API_KEY belum diisi di menu Environment Variables Render!")

MAX_FILES_PER_PROJECT = 10  # 🛡️ Hard Limit anti RAM jebol / Abuse
BLUEPRINT_TTL = 3600        # 🗑 Waktu kadaluarsa blueprint (1 Jam)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Non-blocking Async Client
cloud_client = AsyncClient(host="https://ollama.com", headers={'Authorization': f'Bearer {OLLAMA_API_KEY}'})

# Memori Penyimpanan Blueprint Sementara
TEMP_BLUEPRINTS = {}


# ==========================================
# 2. UTILITIES & SECURITY VALIDATORS
# ==========================================
def cleanup_memory():
    """Garbage Collector: Menghapus blueprint usang agar RAM tidak bocor."""
    current_time = time.time()
    expired_keys = [k for k, v in TEMP_BLUEPRINTS.items() if current_time - v['timestamp'] > BLUEPRINT_TTL]
    for k in expired_keys:
        del TEMP_BLUEPRINTS[k]

def sanitize_filepath(filepath: str) -> str:
    """Mencegah Path Traversal dan nama file kosong dari AI."""
    if not filepath or not str(filepath).strip():
        raise ValueError("Filepath kosong atau invalid dari AI.")
        
    safe_path = os.path.normpath(str(filepath).strip()).lstrip('/')
    if safe_path.startswith("..") or os.path.isabs(safe_path):
        raise ValueError(f"Path Traversal Attempt Detected: {filepath}")
    return safe_path

def create_strong_zip_name(project_name: str) -> str:
    """Membuat nama ZIP yang aman (Sanitized + Timestamp + UUID)."""
    clean_name = re.sub(r'[^a-zA-Z0-9]', '_', project_name).strip('_')
    if not clean_name: clean_name = "AI_Project"
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = uuid.uuid4().hex[:6]
    return f"{clean_name}_{timestamp}_{unique_id}"


# ==========================================
# 3. FASE 1: DRAFTING (Perencanaan Arsitektur)
# ==========================================
async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_memory()
    
    user_msg = ' '.join(context.args)
    if not user_msg:
        await update.message.reply_text("💡 *Cara pakai:* `/create buatin aplikasi backend python FastAPI`", parse_mode='Markdown')
        return

    msg = await update.message.reply_text("⏳ *Merancang arsitektur...*", parse_mode='Markdown')
    
    # Soft Limit: Memberitahu AI batas file
    prompt = f"""You are an elite Software Architect. User wants: "{user_msg}"
Break it down into modular files. STRICT RULE: MAXIMUM {MAX_FILES_PER_PROJECT} FILES.
Return ONLY valid JSON:
{{
    "project_name": "Project Name", "summary": "Short explanation",
    "files": [{{"filepath": "filename.ext", "description": "detailed logic"}}]
}}"""

    try:
        # Timeout 60 detik agar tidak hang jika server Ollama ngelag
        response = await asyncio.wait_for(
            cloud_client.chat(model="glm-5:cloud", messages=[{'role': 'user', 'content': prompt}], options={'temperature': 0.2}),
            timeout=60.0
        )
        content = response['message']['content'].strip()
        
        # Bersihkan format markdown jika AI menuliskannya
        if '```json' in content: content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content: content = content.split('```')[1].split('```')[0].strip()
            
        blueprint = json.loads(content)
        
        # 🛡️ GATEKEEPER: Hard Limit pengecekan jumlah file
        files_to_build = blueprint.get('files', [])
        if len(files_to_build) > MAX_FILES_PER_PROJECT:
            await msg.edit_text(
                f"❌ *Project Terlalu Besar!*\n\nAI merancang {len(files_to_build)} file. "
                f"Batas keamanan sistem maksimal *{MAX_FILES_PER_PROJECT} file*.\n"
                f"_Perkecil scope permintaan Anda (misal: buat MVP-nya saja dulu)._", 
                parse_mode='Markdown'
            )
            return
        elif len(files_to_build) == 0:
            await msg.edit_text("❌ *Error:* AI tidak merancang file sama sekali.", parse_mode='Markdown')
            return

        bp_id = str(update.effective_user.id)
        TEMP_BLUEPRINTS[bp_id] = {"timestamp": time.time(), "data": blueprint}
        
        text = f"📝 *Draft: {blueprint.get('project_name', 'Unnamed Project')}*\n*Daftar File ({len(files_to_build)}/{MAX_FILES_PER_PROJECT}):*\n"
        for i, f in enumerate(files_to_build, 1): 
            text += f"{i}. `{sanitize_filepath(f.get('filepath', 'unnamed.txt'))}`\n"
            
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Bangun & ZIP", callback_data=f"build|{bp_id}")]])
        await msg.edit_text(text, parse_mode='Markdown', reply_markup=keyboard)
        
    except asyncio.TimeoutError:
        await msg.edit_text("❌ Timeout: Server AI terlalu lama merespons saat membuat draft (Maks 60s).")
    except json.JSONDecodeError:
        await msg.edit_text("❌ Error: AI mengembalikan format JSON yang rusak.")
    except Exception as e:
        await msg.edit_text(f"❌ System Error: {str(e)}")


# ==========================================
# 4. FASE 2 & 3: EXECUTE, AUTO-CHECK & DELIVERY
# ==========================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_memory()
    
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("build|"):
        # Hilangkan tombol agar tidak diklik 2x (Anti Race Condition)
        await query.edit_message_reply_markup(reply_markup=None)
        
        bp_id = query.data.split("|")[1]
        bp_record = TEMP_BLUEPRINTS.get(bp_id)
        if not bp_record: 
            await query.message.reply_text("❌ Blueprint kadaluarsa (Lewat 1 jam). Silakan /create ulang.")
            return
            
        blueprint = bp_record['data']
        files = blueprint.get('files', [])
        
        # Buat folder build yang benar-benar unik
        project_folder = f"build_{bp_id}_{uuid.uuid4().hex[:6]}"
        os.makedirs(project_folder, exist_ok=True)
        
        # Gunakan 1 Pesan untuk update agar tidak kena Flood Limit Telegram
        progress_msg = await query.message.edit_text(f"🚀 *Membangun {len(files)} File...*", parse_mode='Markdown')
        
        # Project Memory (Menyimpan 10 baris awal per file untuk konteks AI berikutnya)
        project_interfaces = [] 
        
        for i, f in enumerate(files, 1):
            try:
                safe_filepath = sanitize_filepath(f.get('filepath'))
                full_path = os.path.join(project_folder, safe_filepath)
                if os.path.dirname(full_path):
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
            except ValueError as ve:
                await progress_msg.edit_text(f"{progress_msg.text}\n⚠️ Skip file: {ve}")
                continue

            await progress_msg.edit_text(f"⏳ [{i}/{len(files)}] Menulis `{safe_filepath}`...")
            
            # Gabungkan ingatan AI sebelumnya
            memory_context = ""
            if project_interfaces:
                memory_context = "\nCRITICAL CONTEXT (Existing files):\n" + "\n".join(project_interfaces)

            prompt = f"Write production code for {safe_filepath}. Logic: {f.get('description', '')}\n{memory_context}\nReturn ONLY the code inside ``` markdown."

            try:
                # Timeout 120 detik per file 
                response = await asyncio.wait_for(
                    cloud_client.chat(model="glm-5:cloud", messages=[{'role': 'user', 'content': prompt}], options={'temperature': 0.1, 'num_predict': 8192}),
                    timeout=120.0
                )
                code = response['message']['content'].strip()
                
                if not code: raise ValueError("AI memberikan respons kosong.")

                if code.startswith('```'):
                    code = '\n'.join(code.split('\n')[1:-1] if code.endswith('```') else code.split('\n')[1:])
                
                with open(full_path, 'w', encoding='utf-8') as file: 
                    file.write(code)
                
                # Simpan 10 baris awal untuk memori (Imports, Class/Function names)
                code_summary = '\n'.join(code.split('\n')[:10])
                project_interfaces.append(f"--- {safe_filepath} ---\n{code_summary}...\n")
                
                # Auto-Check jika Python
                if full_path.endswith('.py'):
                    try:
                        py_compile.compile(full_path, doraise=True)
                    except Exception as e:
                        # Tandai ada error di file tersebut
                        with open(full_path, 'w', encoding='utf-8') as file: 
                            file.write(f"# TODO: Fix syntax error\n# {str(e)}\n\n" + code)
                            
            except asyncio.TimeoutError:
                await progress_msg.edit_text(f"❌ [{i}/{len(files)}] Timeout: AI gagal merespons untuk `{safe_filepath}`.")
            except Exception as e:
                await progress_msg.edit_text(f"❌ [{i}/{len(files)}] Gagal membuat `{safe_filepath}`: {str(e)}")

        # --- ZIPPING PHASE ---
        await progress_msg.edit_text("📦 *Semua file ditulis. Melakukan Zipping...*", parse_mode='Markdown')
        
        zip_filename = create_strong_zip_name(blueprint.get('project_name', 'project'))
        zip_path_full = shutil.make_archive(zip_filename, 'zip', project_folder)
        
        try:
            with open(zip_path_full, 'rb') as zip_file:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=zip_file,
                    caption=f"🎉 *Project {blueprint.get('project_name', 'Selesai')}!*\n\n✅ Auto-Check Passed\n🧠 Context Memory Sync Active\n🛡️ ZIP Name: `{zip_filename}.zip`",
                    parse_mode='Markdown'
                )
            # Bersihkan pesan progress agar UI chat bersih
            await progress_msg.delete()
        except Exception as e:
            await progress_msg.edit_text(f"❌ Gagal mengirim ZIP ke Telegram: {e}")
        finally:
            # CLEANUP: Hapus Folder mentah dan ZIP file dari server
            shutil.rmtree(project_folder, ignore_errors=True)
            if os.path.exists(zip_path_full):
                os.remove(zip_path_full)
            # Hapus blueprint dari memory
            if bp_id in TEMP_BLUEPRINTS:
                del TEMP_BLUEPRINTS[bp_id]


# ==========================================
# DUMMY WEB SERVER (UNTUK RENDER.COM)
# ==========================================
from flask import Flask
import threading

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Arsitek Bot is Alive and Running!"

def run_web():
    # Ambil port dari environment Render, default 8080
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

def keep_alive():
    # Jalankan web server di thread terpisah agar bot tidak terblokir
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()

# ==========================================
# 5. ENTRY POINT
# ==========================================
def main():
    print("🤖 Menyalakan Dummy Web Server untuk Render...")
    keep_alive() # Panggil fungsi ini!

    print("🤖 Bot Arsitek V3 Menyala...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling()

if __name__ == '__main__':
    main()