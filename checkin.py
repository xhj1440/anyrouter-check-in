#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 (动态排序 & 资金汇总版)
"""

import asyncio
import hashlib
import json
import os
import sys
import re  # 用于智能排序
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# 假设这些模块在你本地是存在的，保持引用不变
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'

# === 辅助函数 (保持不变) ===
def load_balance_hash():
    try:
        if os.path.exists(BALANCE_HASH_FILE):
            with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception:
        pass
    return None

def save_balance_hash(balance_hash):
    try:
        with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
            f.write(balance_hash)
    except Exception as e:
        print(f'Warning: Failed to save balance hash: {e}')

def generate_balance_hash(balances):
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]

def parse_cookies(cookies_data):
    if isinstance(cookies_data, dict): return cookies_data
    if isinstance(cookies_data, str):
        cookies_dict = {}
        for cookie in cookies_data.split(';'):
            if '=' in cookie:
                key, value = cookie.strip().split('=', 1)
                cookies_dict[key] = value
        return cookies_dict
    return {}

async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
    print(f'[处理中] [{account_name}] 正在获取 WAF cookies...')
    async with async_playwright() as p:
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=False, # 如果在服务器运行建议改为 True，或者确保安装了相关依赖
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                args=['--disable-blink-features=AutomationControlled', '--disable-dev-shm-usage', '--disable-web-security', '--disable-features=VizDisplayCompositor', '--no-sandbox'],
            )
            page = await context.new_page()
            try:
                await page.goto(login_url, wait_until='networkidle')
                try:
                    await page.wait_for_function('document.readyState === "complete"', timeout=5000)
                except Exception:
                    await page.wait_for_timeout(3000)
                
                cookies = await page.context.cookies()
                waf_cookies = {}
                for cookie in cookies:
                    if cookie.get('name') in required_cookies and cookie.get('value'):
                        waf_cookies[cookie.get('name')] = cookie.get('value')
                
                if any(c not in waf_cookies for c in required_cookies):
                    print(f'[失败] [{account_name}] 缺少 WAF cookies')
                    await context.close()
                    return None
                
                print(f'[成功] [{account_name}] WAF cookies 获取成功')
                await context.close()
                return waf_cookies
            except Exception as e:
                print(f'[失败] [{account_name}] Playwright 异常: {e}')
                await context.close()
                return None

def get_user_info(client, headers, user_info_url: str):
    try:
        response = client.get(user_info_url, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                user_data = data.get('data', {})
                # 注意：这里已经是 float 类型
                quota = round(user_data.get('quota', 0) / 500000, 2)
                used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
                return {
                    'success': True,
                    'quota': quota,
                    'used_quota': used_quota,
                    'display': f'💰 当前余额: ${quota}, 已用: ${used_quota}',
                }
        return {'success': False, 'error': f'HTTP {response.status_code}'}
    except Exception as e:
        return {'success': False, 'error': str(e)[:50]}

async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
    if provider_config.needs_waf_cookies():
        login_url = f'{provider_config.domain}{provider_config.login_path}'
        waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
        if not waf_cookies: return None
        return {**waf_cookies, **user_cookies}
    return user_cookies

def execute_check_in(client, account_name: str, provider_config, headers: dict):
    checkin_headers = headers.copy()
    checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})
    try:
        response = client.post(f'{provider_config.domain}{provider_config.sign_in_path}', headers=checkin_headers, timeout=30)
        if response.status_code == 200:
            return True
        return False
    except Exception as e:
        return False

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    print(f'\n[处理中] 开始处理 [{account_name}]')
    
    provider_config = app_config.get_provider(account.provider)
    if not provider_config: return False, {'success': False, 'error': '配置错误'}

    user_cookies = parse_cookies(account.cookies)
    all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
    if not all_cookies: return False, {'success': False, 'error': 'Cookie获取失败'}

    client = httpx.Client(http2=True, timeout=30.0)
    try:
        client.cookies.update(all_cookies)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
            provider_config.api_user_key: account.api_user,
        }
        
        # 先获取用户信息(余额)
        user_info = get_user_info(client, headers, f'{provider_config.domain}{provider_config.user_info_path}')
        if user_info.get('success'):
            print(f"[{account_name}] {user_info['display']}")
        else:
            print(f"[{account_name}] 获取信息失败: {user_info.get('error')}")
        
        # 执行签到
        success = True
        if provider_config.needs_manual_check_in():
            success = execute_check_in(client, account_name, provider_config, headers)
            if success: print(f"[{account_name}] 签到成功")
            else: print(f"[{account_name}] 签到失败")
        else:
            print(f"[{account_name}] 自动签到完成")
            
        return success, user_info
    except Exception as e:
        print(f"[{account_name}] 异常: {e}")
        return False, {'success': False, 'error': str(e)}
    finally:
        client.close()

async def main():
    print('[系统] AnyRouter.top 自动签到 (动态列表排序 + 资金汇总版)')
    print(f'[时间] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    accounts = load_accounts_config()
    if not accounts: sys.exit(1)
    print(f'[信息] 共发现 {len(accounts)} 个账号')

    # === 1. 定义结果列表 & 统计变量 ===
    results_list = []
    success_count = 0
    current_balances = {}
    
    # 新增：总金额统计变量
    total_quota_sum = 0.0
    total_used_sum = 0.0

    # === 2. 遍历执行 ===
    for i, account in enumerate(accounts):
        account_name = account.get_display_name(i)
        account_key = f'account_{i + 1}'
        
        try:
            success, user_info = await check_in_account(account, i, app_config)
            
            if success:
                success_count += 1

            if user_info and user_info.get('success'):
                current_balances[account_key] = {'quota': user_info['quota'], 'used': user_info['used_quota']}
                # 新增：累加金额 (确保是数字)
                total_quota_sum += float(user_info.get('quota', 0))
                total_used_sum += float(user_info.get('used_quota', 0))
                
                msg_content = f"[{account_name}]\n{user_info['display']}"
            else:
                error_msg = user_info.get('error', '未知错误') if user_info else '未知错误'
                msg_content = f"[{account_name}]\n❌ 信息获取失败: {error_msg}"
            
            results_list.append({
                'name': account_name,
                'msg': msg_content
            })

        except Exception as e:
            results_list.append({
                'name': account_name,
                'msg': f"[{account_name}]\n❌ 脚本执行异常: {str(e)[:30]}"
            })

    # === 3. 智能排序 ===
    def natural_key(item):
        text = item['name']
        return int(text) if text.isdigit() else text

    results_list.sort(key=natural_key)

    # === 4. 生成通知 (含汇总) ===
    # 提取排序后的消息文本
    final_content_lines = [item['msg'] for item in results_list]
    
    # 计算总资产
    total_assets = total_quota_sum + total_used_sum
    
    summary = [
        '📊 签到统计:',
        f'✅ 成功: {success_count}/{len(accounts)}',
        f'❌ 失败: {len(accounts) - success_count}/{len(accounts)}',
        '',  # 空行分隔
        '💰 资金汇总:',
        f'💵 可用总余额: ${total_quota_sum:.2f}',
        f'🧾 已用总额: ${total_used_sum:.2f}',
        f'💳 总资产(可用+已用): ${total_assets:.2f}',
    ]
    
    if success_count == len(accounts): 
        summary.append('\n🎉 全员通过！')
    else: 
        summary.append('\n⚠️ 部分失败')

    time_info = f'[时间] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
    
    # 组合最终消息: 时间 -> 明细 -> 汇总
    notify_content = '\n\n'.join([time_info, '\n'.join(final_content_lines), '\n'.join(summary)])
    
    # 保存Hash
    current_balance_hash = generate_balance_hash(current_balances)
    if current_balance_hash: save_balance_hash(current_balance_hash)

    print('\n' + '='*30)
    print(notify_content)
    print('='*30)
    
    # 推送通知
    notify.push_message('AnyRouter 签到通知', notify_content, msg_type='text')
    
    # 只要有成功的就算 exit 0，避免 Github Action 频繁报错
    sys.exit(0 if success_count > 0 else 1)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
