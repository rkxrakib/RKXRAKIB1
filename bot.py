import os
import re
import time
import json
import zipfile
import asyncio
import requests
import shutil
import datetime
import logging
import hashlib
import traceback
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from typing import List, Dict, Any, Optional

# --- FIREBASE ADMIN SDK ---
import firebase_admin
from firebase_admin import credentials, db

# --- TELEGRAM BOT API ---
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    CallbackQueryHandler
)

# =========================================================
# --- CONFIGURATION & CONSTANTS ---
# =========================================================
TOKEN = "8609131343:AAHgqZzGhAoT72S8fwLfPMdhO5PQjiJKt64"
ADMIN_IDS = [7761133429] # আপনার আইডি
DATABASE_URL = "https://rkxrakib-f0c58-default-rtdb.firebaseio.com/" # আপনার ফায়ারবেস ইউআরএল
SERVICE_ACCOUNT_FILE = "service_account.json" # ফায়ারবেস কি ফাইল

# Bot Settings
INITIAL_LIMITS = 10
REFERRAL_BONUS = 1
MAX_RECURSION_DEPTH = 2
MAX_PAGE_COUNT = 40
DOWNLOAD_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================================================
# --- FIREBASE DATABASE CONTROLLER ---
# =========================================================
class FirebaseController:
    """Handles all Realtime Database interactions with Firebase."""
    
    def __init__(self):
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
                firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})
            logger.info("Successfully connected to Firebase.")
        except Exception as e:
            logger.error(f"Firebase Initialization Error: {e}")
            exit(1)

    def get_user(self, uid: str) -> Optional[Dict]:
        return db.reference(f'users/{uid}').get()

    def create_user(self, user_obj, referrer_id: str = None) -> Dict:
        uid = str(user_obj.id)
        now = datetime.datetime.now()
        
        user_data = {
            "uid": uid,
            "first_name": user_obj.first_name,
            "last_name": user_obj.last_name or "",
            "username": user_obj.username or "N/A",
            "limits": INITIAL_LIMITS,
            "referrals": 0,
            "referred_by": referrer_id,
            "total_scraped": 0,
            "joined_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": now.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "active",
            "is_admin": uid in [str(i) for i in ADMIN_IDS]
        }
        db.reference(f'users/{uid}').set(user_data)
        return user_data

    def update_limit(self, uid: str, amount: int):
        ref = db.reference(f'users/{uid}/limits')
        current = ref.get() or 0
        ref.set(current + amount)

    def increment_referral(self, uid: str):
        ref = db.reference(f'users/{uid}/referrals')
        current = ref.get() or 0
        ref.set(current + 1)

    def log_scrape_activity(self, uid: str):
        ref = db.reference(f'users/{uid}/total_scraped')
        current = ref.get() or 0
        ref.set(current + 1)
        db.reference(f'users/{uid}/last_active').set(
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def get_all_users(self) -> Dict:
        return db.reference('users').get() or {}

# Initialize DB
fb = FirebaseController()

# =========================================================
# --- ADVANCED WEB SCRAPER ENGINE ---
# =========================================================
class ProfessionalScraper:
    """Core engine to crawl, extract, and categorize website source data."""
    
    def __init__(self, target_url: str, user_id: str):
        self.target_url = target_url
        self.user_id = user_id
        parsed = urlparse(target_url)
        self.domain = parsed.netloc
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})
        
        # State Management
        self.visited_urls = set()
        self.downloaded_assets = set()
        self.api_endpoints = set()
        self.stats = {
            "pages": 0, "images": 0, "scripts": 0, 
            "styles": 0, "apis": 0, "errors": 0
        }
        
        # Folder Hierarchy
        self.timestamp = int(time.time())
        self.project_id = f"proj_{self.domain.replace('.', '_')}_{self.timestamp}"
        self.root_dir = os.path.join("work_dir", self.project_id)
        self.assets_dir = os.path.join(self.root_dir, "assets")
        self.pages_dir = os.path.join(self.root_dir, "pages")
        self.api_dir = os.path.join(self.root_dir, "api_discoveries")
        
        self._prepare_folders()

    def _prepare_folders(self):
        """Creates an organized directory structure."""
        for path in [self.assets_dir, self.pages_dir, self.api_dir]:
            os.makedirs(path, exist_ok=True)
        for sub in ["css", "js", "img", "fonts"]:
            os.makedirs(os.path.join(self.assets_dir, sub), exist_ok=True)

    def _sanitize_path(self, url_path: str, default_name: str) -> str:
        """Sanitizes URL paths for local file system."""
        if not url_path or url_path == "/":
            return default_name
        name = url_path.strip("/").replace("/", "_")
        return name if name else default_name

    def _detect_apis(self, content: str):
        """Scans content for potential API endpoints using RegEx."""
        patterns = [
            r'["\']([/\w\-]+/(?:api|v1|v2|graphql)/[\w\-/]+)["\']',
            r'fetch\(["\'](https?://[\w\.\-/]+)["\']',
            r'axios\.(?:get|post)\(["\']([\w\.\-/]+)["\']'
        ]
        for p in patterns:
            found = re.findall(p, content)
            for endpoint in found:
                full_url = urljoin(self.target_url, endpoint)
                if full_url not in self.api_endpoints:
                    self.api_endpoints.add(full_url)
                    self.stats["apis"] += 1
                    try:
                        name = hashlib.md5(full_url.encode()).hexdigest()[:8]
                        with open(os.path.join(self.api_dir, f"api_{name}.txt"), "w") as f:
                            f.write(f"Endpoint: {full_url}\nFound in: {self.target_url}")
                    except: pass

    async def _download_resource(self, url: str, category: str):
        """Downloads external resources (js, css, img)."""
        if url in self.downloaded_assets or len(self.downloaded_assets) > 300:
            return
        
        try:
            res = self.session.get(url, timeout=10, stream=True)
            if res.status_code == 200:
                filename = os.path.basename(urlparse(url).path)
                if not filename or "." not in filename:
                    ext = {"js": ".js", "css": ".css", "img": ".png"}.get(category, ".tmp")
                    filename = f"res_{len(self.downloaded_assets)}{ext}"
                
                save_path = os.path.join(self.assets_dir, category, filename)
                with open(save_path, "wb") as f:
                    for chunk in res.iter_content(8192):
                        f.write(chunk)
                
                self.downloaded_assets.add(url)
                self.stats[f"{category if category != 'img' else 'images'}s" if category != 'img' else 'images'] += 1
        except:
            self.stats["errors"] += 1

    async def crawl(self, url: str, depth: int = 0):
        """Main recursive crawler."""
        if depth > MAX_RECURSION_DEPTH or len(self.visited_urls) >= MAX_PAGE_COUNT:
            return
        if url in self.visited_urls or self.domain not in url:
            return

        self.visited_urls.add(url)
        try:
            response = self.session.get(url, timeout=DOWNLOAD_TIMEOUT)
            if response.status_code != 200:
                return

            self.stats["pages"] += 1
            html_content = response.text
            self._detect_apis(html_content)
            
            # Save the HTML file
            page_name = self._sanitize_path(urlparse(url).path, "index")
            if not page_name.endswith(".html"): page_name += ".html"
            with open(os.path.join(self.pages_dir, page_name), "w", encoding="utf-8") as f:
                f.write(html_content)

            soup = BeautifulSoup(html_content, 'html.parser')

            # Extract Assets
            tasks = []
            for script in soup.find_all("script", src=True):
                tasks.append(self._download_resource(urljoin(url, script['src']), "js"))
            for link in soup.find_all("link", rel="stylesheet", href=True):
                tasks.append(self._download_resource(urljoin(url, link['href']), "css"))
            for img in soup.find_all("img", src=True):
                tasks.append(self._download_resource(urljoin(url, img['src']), "img"))
            
            if tasks:
                await asyncio.gather(*tasks)

            # Follow Internal Links
            for a in soup.find_all("a", href=True):
                next_url = urljoin(url, a['href'])
                if self.domain in next_url:
                    await self.crawl(next_url, depth + 1)
                    
        except Exception as e:
            logger.error(f"Error crawling {url}: {e}")
            self.stats["errors"] += 1

    def package_project(self) -> str:
        """Compresses the work directory into a ZIP file."""
        zip_filename = f"{self.project_id}.zip"
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as ziph:
            for root, dirs, files in os.walk(self.root_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, self.root_dir)
                    ziph.write(file_path, arcname)
        
        # Cleanup
        shutil.rmtree(self.root_dir)
        return zip_filename

# =========================================================
# --- BOT COMMAND HANDLERS & LOGIC ---
# =========================================================

async def heartbeat_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    """Indicates the bot is working by showing 'typing' status."""
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
            await asyncio.sleep(4)
        except:
            break

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command and referral system."""
    user = update.effective_user
    uid = str(user.id)
    args = context.args
    
    # Check for existing user
    user_data = fb.get_user(uid)
    is_new = False
    
    if not user_data:
        referrer_id = args[0] if args and args[0].isdigit() else None
        
        # Anti-fraud referral check
        if referrer_id and referrer_id != uid:
            ref_data = fb.get_user(referrer_id)
            if ref_data:
                fb.update_limit(referrer_id, REFERRAL_BONUS)
                fb.increment_referral(referrer_id)
                try:
                    await context.bot.send_message(
                        chat_id=int(referrer_id),
                        text=f"🎁 **Referral Success!**\n{user.first_name} joined. You earned +1 limit!",
                        parse_mode='Markdown'
                    )
                except: pass
        
        user_data = fb.create_user(user, referrer_id)
        is_new = True

    # Main UI
    keyboard = [
        [InlineKeyboardButton("Open App", web_app={"url": "https://rkxrakib.free.nf"})],
        [InlineKeyboardButton("👥 Invite Friends", callback_data="ref_link")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        f"💎 **Welcome to Orange Source Downloader v5**\n\n"
        f"👤 **Name:** {user_data['first_name']}\n"
        f"🔑 **Limits:** `{user_data['limits']}`\n"
        f"👥 **Referrals:** `{user_data['referrals']}`\n"
        f"📅 **Joined:** `{user_data['joined_at']}`\n\n"
        "To download source code, simply **send a URL**.\n"
        "Earn more limits by inviting friends or using the Mini App."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

async def handle_referral_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates referral link for the user."""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    bot_username = context.bot.username
    ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    
    await query.edit_message_text(
        f"🚀 **Your Referral Link:**\n`{ref_link}`\n\n"
        "Share this link with friends. Every new user gives you +1 download limit!",
        parse_mode='Markdown'
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes URLs sent by users."""
    user = update.effective_user
    uid = str(user.id)
    
    user_data = fb.get_user(uid)
    if not user_data:
        await update.message.reply_text("Please /start the bot first.")
        return

    # Limit Check
    if user_data['limits'] <= 0:
        await update.message.reply_text(
            "❌ **Limit Reached!**\n\nYou have 0 limits left. Refer a friend or complete tasks in the app to earn more.",
            parse_mode='Markdown'
        )
        return

    # URL Extraction
    text = update.message.text
    found_urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text)
    
    if not found_urls:
        await update.message.reply_text("❌ Please send a valid URL starting with http/https.")
        return

    target_url = found_urls[0]
    status_msg = await update.message.reply_text(
        f"⏳ **Initializing Scraper...**\n`{target_url}`\n\nPlease wait, this may take a minute.",
        parse_mode='Markdown'
    )

    # Start Heartbeat & Scraping
    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(heartbeat_typing(context, update.effective_chat.id, stop_event))

    try:
        scraper = ProfessionalScraper(target_url, uid)
        await scraper.crawl(target_url)
        zip_file = await asyncio.to_thread(scraper.package_project)
        
        # Stop typing
        stop_event.set()
        await typing_task

        # Database Updates
        fb.update_limit(uid, -1)
        fb.log_scrape_activity(uid)
        
        file_size = os.path.getsize(zip_file) / (1024 * 1024)
        stats_caption = (
            f"✅ **Source Code Extracted!**\n\n"
            f"🌐 **Domain:** `{scraper.domain}`\n"
            f"📄 **Pages:** {scraper.stats['pages']}\n"
            f"🖼️ **Images:** {scraper.stats['images']}\n"
            f"📜 **JS/CSS:** {scraper.stats['scripts'] + scraper.stats['styles']}\n"
            f"🔌 **APIs Found:** {scraper.stats['apis']}\n"
            f"📦 **Size:** {file_size:.2f} MB\n\n"
            f"🔑 **Limits Left:** `{user_data['limits'] - 1}`"
        )

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=open(zip_file, 'rb'),
            caption=stats_caption,
            filename=f"{scraper.domain}_source.zip",
            parse_mode='Markdown'
        )
        
        # Cleanup
        if os.path.exists(zip_file): os.remove(zip_file)
        await status_msg.delete()

    except Exception as e:
        stop_event.set()
        logger.error(f"Scraping failed: {e}\n{traceback.format_exc()}")
        await status_msg.edit_text(f"❌ **Error:** Scraper failed to process this site. {str(e)}")

# =========================================================
# --- ADMIN FUNCTIONALITY ---
# =========================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays detailed user report for admins."""
    uid = str(update.effective_user.id)
    if uid not in [str(i) for i in ADMIN_IDS]: return

    users = fb.get_all_users()
    if not users:
        await update.message.reply_text("No users found.")
        return

    report = "📊 **Professional User Report**\n\n"
    report += "UID | Name | Limit | Ref | Scraped\n"
    report += "---------------------------------\n"
    
    for _, d in users.items():
        line = f"`{d['uid']}` | {d['first_name']} | {d['limits']} | {d['referrals']} | {d['total_scraped']}\n"
        if len(report) + len(line) > 4000:
            await update.message.reply_text(report, parse_mode='Markdown')
            report = ""
        report += line
    
    await update.message.reply_text(report, parse_mode='Markdown')

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message to all bot users."""
    uid = str(update.effective_user.id)
    if uid not in [str(i) for i in ADMIN_IDS]: return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    message = " ".join(context.args)
    users = fb.get_all_users()
    
    sent_count = 0
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=int(user_id), text=f"📢 **Notification**\n\n{message}", parse_mode='Markdown')
            sent_count += 1
            await asyncio.sleep(0.05)
        except: pass
    
    await update.message.reply_text(f"✅ Broadcast sent to {sent_count} users.")

# =========================================================
# --- MAIN APPLICATION ENTRY POINT ---
# =========================================================

def main():
    # Workspace Cleanup
    if not os.path.exists("work_dir"): os.makedirs("work_dir")
    
    # Build Application
    application = Application.builder().token(TOKEN).build()

    # Register Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CallbackQueryHandler(handle_referral_query, pattern="^ref_link$"))
    
    # Message Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    print("--- Orange Source Pro v5 System Started ---")
    print(f"Logged in as Admin IDs: {ADMIN_IDS}")
    application.run_polling()

if __name__ == "__main__":
    main()