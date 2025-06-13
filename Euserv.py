"""
功能:
    使用带带弟弟ocr自动识别验证码
    发送通知到微信和Telegram（仅在续费成功或异常时）
    对接gmail邮箱IMAP自动读取PIN
    每天0点和12点自动运行（不依赖schedule）

安装依赖:
    pip install pytz requests beautifulsoup4 ddddocr python-telegram-bot aiohttp --break-system-packages

建议在Linux上使用screen或systemd后台运行
"""

import re
import json
import time
import base64
import imaplib
import email
import logging
import sys
import os
import ddddocr
import requests
from bs4 import BeautifulSoup
from email.header import decode_header
import datetime
from datetime import datetime, timedelta
import pytz
from telegram import Bot
import aiohttp
import asyncio
import signal

# 禁用 ddddocr 的启动信息
logging.getLogger("ddddocr").setLevel(logging.WARNING)

# 设置日志文件，防止日志无限增长
LOG_FILE = "/root/euserv_renewal.log"
def setup_logging():
    """设置日志文件，限制大小"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )
    # 限制日志文件大小（例如 10MB）
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 10 * 1024 * 1024:
        with open(LOG_FILE, "w") as f:
            f.truncate(0)

# 账户信息：用户名和密码
USERNAME = ''  # 德鸡登录用户名
PASSWORD = ''  # 德鸡登录密码

# wxpusher 配置
WXPUSHER_TOKEN = ""  # 在WxPusher平台创建应用后获得
WXPUSHER_TOPIC_ID = ""  # 你的主题ID

# Telegram 配置
TELEGRAM_BOT_TOKEN = ""  # 替换为你的 Telegram Bot Token
TELEGRAM_CHAT_ID = ""  # 替换为你的 Telegram Chat ID

# Gmail IMAP 配置
GMAIL_USER = ''      # Gmail邮箱
GMAIL_APP_PASSWORD = ''  # 需要在Gmail设置中生成应用专用密码
GMAIL_FOLDER = "INBOX"  # 邮件文件夹
IMAP_SERVER = "imap.gmail.com" #服务器地址
IMAP_PORT = 993  #端口

# 最大登录重试次数
LOGIN_MAX_RETRY_COUNT = 10  # 增加重试次数

# 接收 PIN 的等待时间，单位为秒
WAITING_TIME_OF_PIN = 15

# 创建一个全局的 ddddocr 实例
ocr = ddddocr.DdddOcr(show_ad=False)  # 禁用广告显示

# 更新 User-Agent 为更新的浏览器版本
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 初始化全局变量
renewal_performed = False
last_execution_date = None
desp = ""

def log(info: str):
    """日志记录函数"""
    emoji_map = {
        "正在续费": "🔄",
        "检测到": "🔍",
        "ServerID": "🔗",
        "无需更新": "✅",
        "续订错误": "⚠️",
        "已成功续订": "🎉",
        "所有工作完成": "🏁",
        "登陆失败": "❗",
        "验证通过": "✔️",
        "验证失败": "❌",
        "验证码是": "🔢",
        "登录尝试": "🔑",
        "[Gmail]": "📧",
        "[ddddocr]": "🧩",
        "[德鸡自动续期]": "🌐",
    }
    # 对每个关键字进行检查，并在找到时添加 emoji
    for key, emoji in emoji_map.items():
        if key in info:
            info = emoji + " " + info
            break

    logging.info(info)
    
    global desp
    desp += info + "\n\n"

# 登录重试机制
def login_retry(max_retry=3):# 默认重试 3 次
    def wrapper(func):
        def inner(*args, **kwargs):
            ret, ret_session = func(*args, **kwargs)
            number = 0
            if ret == "-1":
                while number < max_retry:
                    number += 1
                    if number > 1:
                        log(f"[德鸡自动续期] 登录尝试第 {number} 次")
                    sess_id, session = func(*args, **kwargs)
                    if sess_id != "-1":
                        return sess_id, session
                    else:
                        if number == max_retry:
                            return sess_id, session
                    time.sleep(2)  # 增加延迟，避免请求过快
            else:
                return ret, ret_session
        return inner
    return wrapper
    
# 登录函数
@login_retry(max_retry=LOGIN_MAX_RETRY_COUNT)
def login(username: str, password: str) -> (str, requests.session):
    """登录函数"""
    headers = {"user-agent": user_agent, "origin": "https://www.euserv.com"}
    url = "https://support.euserv.com/index.iphp"
    ddddocr_image_url = "https://support.euserv.com/securimage_show.php"
    session = requests.Session()

    # 增加初始请求延迟
    log("[德鸡自动续期] 正在获取登录页面...")
    sess = session.get(url, headers=headers)
    sess_id = re.findall("PHPSESSID=(\\w{10,100});", str(sess.headers))[0]
    log(f"[德鸡自动续期] 获取到 PHPSESSID: {sess_id}")
    
    # 模拟浏览器行为，获取 logo 图片
    session.get("https://support.euserv.com/pic/logo_small.png", headers=headers)
    time.sleep(1)  # 增加延迟

    login_data = {
        "email": username,
        "password": password,
        "form_selected_language": "en",
        "Submit": "Login",
        "subaction": "login",
        "sess_id": sess_id,
    }
    log("[德鸡自动续期] 正在提交登录请求...")
    f = session.post(url, headers=headers, data=login_data)
    f.raise_for_status()

    if "Hello" not in f.text and "Confirm or change your customer data here" not in f.text:
        if "To finish the login process please solve the following captcha." not in f.text:
            log(f"[德鸡自动续期] 登录失败，响应内容: {f.text}")
            return "-1", session
        else:
            log("[ddddocr] 检测到验证码，正在进行验证码识别...")
            ddddocr_code = ddddocr_solver(ddddocr_image_url, session)
            log("[ddddocr] 识别的验证码是: {}".format(ddddocr_code))

            f2 = session.post(
                url,
                headers=headers,
                data={
                    "subaction": "login",
                    "sess_id": sess_id,
                    "captcha_code": ddddocr_code,
                },
            )
            if "To finish the login process please solve the following captcha." not in f2.text:
                log("[ddddocr] 验证通过")
                return sess_id, session
            else:
                log("[ddddocr] 验证失败")
                log(f"[ddddocr] 完整响应: {f2.text}")
                return "-1", session
    else:
        log("[德鸡自动续期] 登录成功")
        return sess_id, session
        
# 使用 ddddocr 识别验证码
def ddddocr_solver(ddddocr_image_url: str, session: requests.session) -> str:
    log("[ddddocr] 正在下载验证码图片...")
    response = session.get(ddddocr_image_url)
    log("[ddddocr] 验证码图片下载完成，开始识别...")
    result = ocr.classification(response.content)
    return result
    
# 从 Gmail 获取 PIN
def get_pin_from_gmail() -> str:
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) # IMAP方式连接到Gmail邮箱
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)       #执行登录

    mail.select(GMAIL_FOLDER) # 选择收件箱文件夹
        
    status, messages = mail.search(None, "ALL")  # 搜索最新的邮件
    if status != "OK":
        log("[Gmail] 无法检索邮件列表")
        return None

    latest_email_id = messages[0].split()[-1]   # 获取最新邮件的 ID
    status, msg_data = mail.fetch(latest_email_id, "(RFC822)")  # 获取邮件内容
    if status != "OK":
        log("[Gmail] 无法检索邮件内容")
        return None

    raw_email = msg_data[0][1]  # 解析邮件内容
    msg = email.message_from_bytes(raw_email)
    
    pin = None  
    
    # 提取邮件正文
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                body = part.get_payload(decode=True).decode()
                pin_match = re.search(r'PIN:\s*(\d{6})', body) # 使用正则表达式提取PIN
                if pin_match:
                    pin = pin_match.group(1)
                    break
    else:
        body = msg.get_payload(decode=True).decode()
        pin_match = re.search(r'PIN:\s*(\d{6})', body)  # 使用正则表达式提取PIN
        if pin_match:
            pin = pin_match.group(1)

    mail.logout() # 退出邮箱连接

    if pin:
        log(f"[Gmail] 成功获取PIN: {pin}")
        return pin
    else:
        raise Exception("未能从邮件中提取PIN")

def get_servers(sess_id: str, session: requests.session) -> {}:
    """获取服务器列表"""
    d = {}
    url = "https://support.euserv.com/index.iphp?sess_id=" + sess_id
    headers = {"user-agent": user_agent, "origin": "https://www.euserv.com"}
    f = session.get(url=url, headers=headers)
    f.raise_for_status()
    soup = BeautifulSoup(f.text, "html.parser")
    for tr in soup.select(
        "#kc2_order_customer_orders_tab_content_1 .kc2_order_table.kc2_content_table tr"
    ):
        server_id = tr.select(".td-z1-sp1-kc")
        if not len(server_id) == 1:
            continue
        flag = (
            True
            if tr.select(".td-z1-sp2-kc .kc2_order_action_container")[0]
            .get_text()
            .find("Contract extension possible from")
            == -1
            else False
        )
        d[server_id[0].get_text()] = flag
    return d
    
# 发送 WxPusher 通知
async def send_wxpusher_notification(message: str):
    """发送微信通知"""
    data = {
        "appToken": WXPUSHER_TOKEN,
        "content": message,
        "contentType": 2,  # 1表示文本，2表示HTML
        "topicIds": [int(WXPUSHER_TOPIC_ID)],
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "http://wxpusher.zjiecode.com/api/send/message",
                json=data
            ) as response:
                if response.status != 200:
                    log("[德鸡自动续期] WxPusher 推送失败")
                else:
                    log("[德鸡自动续期] 续期结果已推送至微信")
        except Exception as e:
            log(f"[德鸡自动续期] 发送WxPusher通知时发生错误: {str(e)}")

# 发送 Telegram 通知（仅关键信息）
async def send_telegram_notification(message: str):
    """发送Telegram通知"""
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        log("[德鸡自动续期] 续期结果已推送至Telegram")
    except Exception as e:
        log(f"[德鸡自动续期] 发送Telegram通知时发生错误: {str(e)}")

# 续期操作
def renew(sess_id: str, session: requests.session, password: str, order_id: str) -> bool:
    global renewal_performed
    
    url = "https://support.euserv.com/index.iphp"
    headers = {
        "user-agent": user_agent,
        "Host": "support.euserv.com",
        "origin": "https://support.euserv.com",
        "Referer": "https://support.euserv.com/index.iphp",
    }
    data = {
        "Submit": "Extend contract",
        "sess_id": sess_id,
        "ord_no": order_id,
        "subaction": "choose_order",
        "choose_order_subaction": "show_contract_details",
    }
    session.post(url, headers=headers, data=data)

    # 触发PIN发送
    session.post(
        url,
        headers=headers,
        data={
            "sess_id": sess_id,
            "subaction": "show_kc2_security_password_dialog",
            "prefix": "kc2_customer_contract_details_extend_contract_",
            "type": "1",
        },
    )

    log("[Gmail] 等待PIN邮件到达...")
    time.sleep(WAITING_TIME_OF_PIN)
        
    retry_count = 3
    pin = None
    for i in range(retry_count):
        try:
            pin = get_pin_from_gmail()
            if pin:
                break
        except Exception as e:
            if i < retry_count - 1:
                log(f"[Gmail] 第{i+1}次尝试获取PIN失败，等待后重试...")
                time.sleep(5)
            else:
                raise Exception(f"多次尝试获取PIN均失败: {str(e)}")
        
    if not pin:
        return False

    # 使用PIN获取token
    data = {
        "auth": pin,
        "sess_id": sess_id,
        "subaction": "kc2_security_password_get_token",
        "prefix": "kc2_customer_contract_details_extend_contract_",
        "type": 1,
        "ident": f"kc2_customer_contract_details_extend_contract_{order_id}",
    }
    f = session.post(url, headers=headers, data=data)
    f.raise_for_status()
    
    if not json.loads(f.text)["rs"] == "success":
        return False
        
    token = json.loads(f.text)["token"]["value"]
    data = {
        "sess_id": sess_id,
        "ord_id": order_id,
        "subaction": "kc2_customer_contract_details_extend_contract_term",
        "token": token,
    }
    
    response = session.post(url, headers=headers, data=data)
    if response.status_code == 200:
        renewal_performed = True
        return True
    return False

# 检查续期状态
def check(sess_id: str, session: requests.session):
    log("[德鸡自动续期] 正在检查续期状态...")
    d = get_servers(sess_id, session)
    flag = True
    for key, val in d.items():
        if val:
            flag = False
            log("[德鸡自动续期] ServerID: %s 续期失败!" % key)

    if flag:
        log("[德鸡自动续期] 所有德鸡续期完成。开启挂机人生！")

# 处理续期流程
async def process_renewal():
    global renewal_performed, desp, last_execution_date
    renewal_performed = False
    desp = ""  # 清空日志
    
    if not USERNAME or not PASSWORD:
        log("[德鸡自动续期] 你没有添加任何账户")
        return
        
    user_list = USERNAME.strip().split()
    passwd_list = PASSWORD.strip().split()
    if len(user_list) != len(passwd_list):
        log("[德鸡自动续期] 用户名和密码数量不匹配!")
        return

    try:
        for i in range(len(user_list)):
            log("[德鸡自动续期] 正在续费第 %d 个账号" % (i + 1))
            sessid, s = login(user_list[i], passwd_list[i])
            
            if sessid == "-1":
                log("[德鸡自动续期] 第 %d 个账号登陆失败，请检查登录信息" % (i + 1))
                continue
                
            SERVERS = get_servers(sessid, s)
            log("[德鸡自动续期] 检测到第 {} 个账号有 {} 台 VPS，正在尝试续期".format(i + 1, len(SERVERS)))
            
            for k, v in SERVERS.items():
                if v:
                    try:
                        if not renew(sessid, s, passwd_list[i], k):
                            log("[德鸡自动续期] ServerID: %s 续订错误!" % k)
                        else:
                            log("[德鸡自动续期] ServerID: %s 已成功续订!" % k)
                    except Exception as e:
                        log(f"[德鸡自动续期] 续订 ServerID: {k} 时发生错误: {str(e)}")
                else:
                    log("[德鸡自动续期] ServerID: %s 无需更新" % k)
            
            time.sleep(15)
            check(sessid, s)
            time.sleep(5)

        # 发送通知（仅在续费成功时）
        if renewal_performed:
            tg_message = "<b>德鸡续期成功</b>\n续费完成"
            wx_message = "<b>德鸡续期成功</b>\n\n" + desp
            await send_telegram_notification(tg_message)
            if WXPUSHER_TOKEN and WXPUSHER_TOPIC_ID:
                await send_wxpusher_notification(wx_message)
        # 无需续费时不发送通知，仅记录日志

    except Exception as e:
        error_msg = f"[德鸡自动续期] 续期过程发生错误: {str(e)}"
        log(error_msg)
        tg_message = f"<b>德鸡续期错误</b>\n{error_msg}"
        wx_message = f"<b>德鸡续期错误</b>\n{error_msg}\n\n{desp}"
        await send_telegram_notification(tg_message)
        if WXPUSHER_TOKEN and WXPUSHER_TOPIC_ID:
            await send_wxpusher_notification(wx_message)

# 计算下一次运行时间
def get_next_run_time():
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    current_second = now.second

    # 如果当前时间在 0:00 - 11:59，下一运行时间是今天 12:00
    if current_hour < 12:
        next_run = now.replace(hour=12, minute=0, second=0, microsecond=0)
    # 如果当前时间在 12:00 - 23:59，下一运行时间是明天 0:00
    else:
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    return next_run

# 主函数 - 定时运行
async def main():
    log("[德鸡自动续期] 脚本启动")
    log(f"[德鸡自动续期] Python executable: {sys.executable}")
    log(f"[德鸡自动续期] sys.path: {sys.path}")
    
    while True:
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        current_second = now.second

        # 检查是否到达 0 点或 12 点
        if (current_hour == 0 or current_hour == 12) and current_minute == 0 and current_second == 0:
            log("[德鸡自动续期] 当前时间为 {}，开始执行续期流程".format(now.strftime("%H:%M")))
            await process_renewal()
            log("[德鸡自动续期] 续期流程执行完成")
            time.sleep(60)  # 等待 1 分钟，避免重复执行
        else:
            # 计算下一次运行时间
            next_run = get_next_run_time()
            seconds_until_next_run = (next_run - now).total_seconds()
            log("[德鸡自动续期] 下次运行时间: {}，将在 {} 秒后执行".format(
                next_run.strftime("%Y-%m-%d %H:%M:%S"), int(seconds_until_next_run)))
            time.sleep(seconds_until_next_run)

def handle_exit(signum, frame):
    """处理退出信号"""
    log("[德鸡自动续期] 收到退出信号，正在关闭守护进程...")
    sys.exit(0)

if __name__ == "__main__":
    try:
        # 初始化日志
        setup_logging()
        
        # 检查 Telegram 配置
        if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or TELEGRAM_CHAT_ID == "YOUR_TELEGRAM_CHAT_ID":
            log("[德鸡自动续期] 请配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
            sys.exit(1)

        # 检查依赖
        required_modules = ['pytz', 'requests', 'bs4', 'ddddocr', 'telegram', 'aiohttp']  # 修改 beautifulsoup4 为 bs4
        missing_modules = []
        for module in required_modules:
            try:
                __import__(module)
            except ImportError:
                missing_modules.append(module)
        if missing_modules:
            log(f"[德鸡自动续期] 缺少以下依赖: {', '.join(missing_modules)}")
            log("[德鸡自动续期] 请安装依赖: pip3 install " + " ".join(missing_modules) + " -i https://pypi.tuna.tsinghua.edu.cn/simple")
            sys.exit(1)

        # 注册信号处理
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
        
        # 启动主函数
        asyncio.run(main())
    except Exception as e:
        log(f"[德鸡自动续期] 程序异常退出: {str(e)}")
        sys.exit(1)