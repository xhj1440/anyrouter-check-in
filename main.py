import requests
import os
import json
import time
# --- 基础配置 ---
# 请确认这个地址是你想签到的网站地址
BASE_URL = "https://anyrouter.com" 
URL_CHECKIN = f"{BASE_URL}/api/v1/checkin"
# 伪装浏览器
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json"
}
def get_user_balance(user_id, cookie_str):
    """
    查询用户余额
    """
    url = f"{BASE_URL}/api/v1/users/{user_id}"
    headers = COMMON_HEADERS.copy()
    headers["Cookie"] = cookie_str
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # 尝试获取 credit 字段，如果不在 data 里就在最外层找
            balance = data.get('data', {}).get('credit')
            if balance is None:
                balance = data.get('credit', '未知')
            return f"{balance}"
        else:
            return "获取失败"
    except Exception:
        return "查询出错"
def run_task(account):
    """
    执行单账号任务：签到 + 查余额
    """
    name = account.get('name', '未知用户')
    user_id = account.get('api_user')
    
    # 提取 session
    cookies_dict = account.get('cookies', {})
    session_val = cookies_dict.get('session')
    
    if not session_val:
        return f"⚠️ {name}: Cookie 缺失"
    # 拼装 Cookie
    cookie_str = f"session={session_val}"
    headers = COMMON_HEADERS.copy()
    headers["Cookie"] = cookie_str
    # --- 1. 签到 ---
    checkin_msg = ""
    try:
        r = requests.post(URL_CHECKIN, headers=headers, json={}, timeout=10)
        if r.status_code == 200:
            res = r.json()
            msg = res.get('message', 'OK')
            if "已签到" in msg or "成功" in msg:
                 checkin_msg = "✅ 签到成功"
            else:
                 checkin_msg = f"👌 {msg}"
        else:
            checkin_msg = f"❌ 签到失败({r.status_code})"
    except:
        checkin_msg = "❌ 请求异常"
    # --- 2. 查余额 ---
    balance_msg = "余额: --"
    if user_id:
        bal = get_user_balance(user_id, cookie_str)
        balance_msg = f"💰 余额: {bal}"
    
    return f"{name} | {checkin_msg} | {balance_msg}"
def send_feishu(lines):
    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        print("未配置 FEISHU_WEBHOOK，跳过通知")
        return
    content = "AnyRouter 监控日报\n" + "-"*20 + "\n" + "\n".join(lines)
    data = {"msg_type": "text", "content": {"text": content}}
    requests.post(webhook, json=data)
if __name__ == "__main__":
    # 从 Secret 读取账号列表
    json_str = os.environ.get("COOKIES_JSON")
    
    if not json_str:
        print("❌ 错误：未检测到 COOKIES_JSON 变量")
        exit(1)
    try:
        accounts = json.loads(json_str)
    except:
        print("❌ 错误：JSON 格式解析失败，请检查 Secret 格式")
        exit(1)
        
    print(f"🚀 开始执行 {len(accounts)} 个账号的任务...")
    
    report_lines = []
    for acc in accounts:
        line = run_task(acc)
        print(line)
        report_lines.append(line)
        time.sleep(1) # 稍微暂停一下，防止请求太快
        
    send_feishu(report_lines)
    print("🏁 所有任务执行完毕")
