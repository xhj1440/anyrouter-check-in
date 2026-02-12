#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 (汉化 & 强迫症排序版)
"""

import asyncio
import hashlib
import json
import os
import sys
import re  # 引入正则用于排序
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'


def load_balance_hash():
    """加载余额hash"""
    try:
        if os.path.exists(BALANCE_HASH_FILE):
            with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def save_balance_hash(balance_hash):
    """保存余额hash"""
    try:
        with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
            f.write(balance_hash)
    except Exception as e:
        print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
    """生成余额数据的hash"""
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
    """解析 cookies 数据"""
    if isinstance(cookies_data, dict):
        return cookies_data

    if isinstance(cookies_data, str):
        cookies_dict = {}
        for cookie in cookies_data.split(';'):
            if '=' in cookie:
                key, value = cookie.strip().split('=', 1)
                cookies_dict[key] = value
        return cookies_dict
    return {}


async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
    """使用 Playwright 获取 WAF cookies（隐私模式）"""
    print(f'[处理中] {account_name}: 正在启动浏览器获取 WAF cookies...')

    async with async_playwright() as p:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=False, # 如果在服务器运行报错，可能需要改为 True
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--no-sandbox',
                ],
            )

            page = await context.new_page()

            try:
                print(f'[处理中] {account_name}: 正在访问登录页...')

                await page.goto(login_url, wait_until='networkidle')

                try:
                    await page.wait_for_function('document.readyState === "complete"', timeout=5000)
                except Exception:
                    await page.wait_for_timeout(3000)

                cookies = await page.context.cookies()

                waf_cookies = {}
                for cookie in cookies:
                    cookie_name = cookie.get('name')
                    cookie_value = cookie.get('value')
                    if cookie_name in required_cookies and cookie_value is not None:
                        waf_cookies[cookie_name] = cookie_value

                print(f'[信息] {account_name}: 获取到 {len(waf_cookies)} 个 WAF cookies')

                missing_cookies = [c for c in required_cookies if c not in waf_cookies]

                if missing_cookies:
                    print(f'[失败] {account_name}: 缺少 WAF cookies: {missing_cookies}')
                    await context.close()
                    return None

                print(f'[成功] {account_name}: 成功获取所有 WAF cookies')

                await context.close()

                return waf_cookies

            except Exception as e:
                print(f'[失败] {account_name}: 获取 WAF cookies 时发生错误: {e}')
                await context.close()
                return None


def get_user_info(client, headers, user_info_url: str):
    """获取用户信息"""
    try:
        response = client.get(user_info_url, headers=headers, timeout=30)

        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                user_data = data.get('data', {})
                quota = round(user_data.get('quota', 0) / 500000, 2)
                used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
                return {
                    'success': True,
                    'quota': quota,
                    'used_quota': used_quota,
                    # --- 汉化部分 ---
                    'display': f'💰 当前余额: ${quota}, 已用: ${used_quota}',
                }
        return {'success': False, 'error': f'获取用户信息失败: HTTP {response.status_code}'}
    except Exception as e:
        return {'success': False, 'error': f'获取用户信息失败: {str(e)[:50]}...'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
    """准备请求所需的 cookies"""
    waf_cookies = {}

    if provider_config.needs_waf_cookies():
        login_url = f'{provider_config.domain}{provider_config.login_path}'
        waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
        if not waf_cookies:
            print(f'[失败] {account_name}: 无法获取 WAF cookies')
            return None
    else:
        print(f'[信息] {account_name}: 不需要 WAF 绕过，直接使用用户 cookies')

    return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict):
    """执行签到请求"""
    print(f'[网络] {account_name}: 正在执行签到...')

    checkin_headers = headers.copy()
    checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

    sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
    response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

    print(f'[响应] {account_name}: 状态码 {response.status_code}')

    if response.status_code == 200:
        try:
            result = response.json()
            if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
                print(f'[成功] {account_name}: 签到成功!')
                return True
            else:
                error_msg = result.get('msg', result.get('message', 'Unknown error'))
                print(f'[失败] {account_name}: 签到失败 - {error_msg}')
                return False
        except json.JSONDecodeError:
            if 'success' in response.text.lower():
                print(f'[成功] {account_name}: 签到成功!')
                return True
            else:
                print(f'[失败] {account_name}: 签到失败 - 无效的响应格式')
                return False
    else:
        print(f'[失败] {account_name}: 签到失败 - HTTP {response.status_code}')
        return False


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    """为单个账号执行签到操作"""
    account_name = account.get_display_name(account_index)
    print(f'\n[处理中] 开始处理 {account_name}')

    provider_config = app_config.get_provider(account.provider)
    if not provider_config:
        print(f'[失败] {account_name}: 未找到提供商 "{account.provider}" 的配置')
        return False, None

    print(f'[信息] {account_name}: 使用提供商 "{account.provider}" ({provider_config.domain})')

    user_cookies = parse_cookies(account.cookies)
    if not user_cookies:
        print(f'[失败] {account_name}: 配置格式无效')
        return False, None

    all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
    if not all_cookies:
        return False, None

    client = httpx.Client(http2=True, timeout=30.0)

    try:
        client.cookies.update(all_cookies)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Referer': provider_config.domain,
            'Origin': provider_config.domain,
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            provider_config.api_user_key: account.api_user,
        }

        user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
        user_info = get_user_info(client, headers, user_info_url)
        if user_info and user_info.get('success'):
            print(user_info['display'])
        elif user_info:
            print(user_info.get('error', '未知错误'))

        if provider_config.needs_manual_check_in():
            success = execute_check_in(client, account_name, provider_config, headers)
            return success, user_info
        else:
            print(f'[信息] {account_name}: 自动完成签到 (通过用户信息请求触发)')
            return True, user_info

    except Exception as e:
        print(f'[失败] {account_name}: 签到过程中发生错误 - {str(e)[:50]}...')
        return False, None
    finally:
        client.close()


async def main():
    """主函数"""
    print('[系统] AnyRouter.top 多账号自动签到脚本启动 (汉化排序版)')
    print(f'[时间] 执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    print(f'[信息] 加载了 {len(app_config.providers)} 个提供商配置')

    accounts = load_accounts_config()
    if not accounts:
        print('[失败] 无法加载账号配置，程序退出')
        sys.exit(1)

    print(f'[信息] 发现 {len(accounts)} 个账号配置')

    last_balance_hash = load_balance_hash()

    success_count = 0
    total_count = len(accounts)
    notification_content = []
    current_balances = {}
    
    # === 核心修改：强制开启通知和余额变化 ===
    need_notify = True 
    balance_changed = True 
    # ==================================

    for i, account in enumerate(accounts):
        account_key = f'account_{i + 1}'
        try:
            success, user_info = await check_in_account(account, i, app_config)
            if success:
                success_count += 1

            should_notify_this_account = False

            # 如果失败了，这里会先收集一次（作为失败记录）
            if not success:
                should_notify_this_account = True
                account_name = account.get_display_name(i)
                print(f'[通知] {account_name} 失败，将发送通知')

            # 记录当前余额
            if user_info and user_info.get('success'):
                current_quota = user_info['quota']
                current_used = user_info['used_quota']
                current_balances[account_key] = {'quota': current_quota, 'used': current_used}

            if should_notify_this_account:
                account_name = account.get_display_name(i)
                status = '[成功]' if success else '[失败]'
                account_result = f'{status} {account_name}'
                if user_info and user_info.get('success'):
                    account_result += f'\n{user_info["display"]}'
                elif user_info:
                    account_result += f'\n{user_info.get("error", "未知错误")}'
                notification_content.append(account_result)

        except Exception as e:
            account_name = account.get_display_name(i)
            print(f'[失败] {account_name} 处理异常: {e}')
            notification_content.append(f'[失败] {account_name} 异常: {str(e)[:50]}...')

    # 生成 Hash 用于本地缓存（虽然我们强制发通知，但还是保留这个逻辑以免报错）
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    if current_balance_hash:
        save_balance_hash(current_balance_hash)

    # === 收集所有余额信息（无论是否有变化） ===
    # 因为 balance_changed 被强制为 True，所以这里一定会执行
    if balance_changed:
        for i, account in enumerate(accounts):
            account_key = f'account_{i + 1}'
            account_name = account.get_display_name(i)
            
            # 如果成功获取到了余额
            if account_key in current_balances:
                account_result = f'[余额] {account_name}'
                account_result += f'\n💰 当前余额: ${current_balances[account_key]["quota"]}, 已用: ${current_balances[account_key]["used"]}'
                
                # 检查是否重复：有些账号如果签到失败被添加过了，这里避免重复添加
                # 但由于失败通常没余额，所以这里主要添加成功的
                if not any(account_name in item for item in notification_content):
                    notification_content.append(account_result)

    if need_notify and notification_content:
        
        # === 强迫症排序逻辑 (Start) ===
        def natural_sort_key(text):
            # 提取通知的第一行（通常包含名字，如 "[余额] 10"）
            first_line = text.split('\n')[0]
            # 尝试找到名字部分。我们移除 [余额] [成功] 等前缀
            # 正则匹配：任意中括号内容 + 空格 + (名字)
            import re
            match = re.search(r'\[.*?\]\s*(.*)', first_line)
            if match:
                name = match.group(1).strip()
                # 如果名字是纯数字（例如 "10"），转成整数进行数字排序
                if name.isdigit():
                    return int(name)
                # 否则按字符串排序
                return name
            return text # 匹配不到就原样排

        # 对所有消息进行重新排序
        notification_content.sort(key=natural_sort_key)
        # === 强迫症排序逻辑 (End) ===

        # 构建中文通知摘要
        summary = [
            '📊 签到统计:',
            f'✅ 成功: {success_count}/{total_count}',
            f'❌ 失败: {total_count - success_count}/{total_count}',
        ]

        if success_count == total_count:
            summary.append('🎉 所有账号签到成功！')
        elif success_count > 0:
            summary.append('⚠️ 部分账号签到成功')
        else:
            summary.append('🛑 所有账号签到失败')

        time_info = f'[时间] 执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

        notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

        print(notify_content)
        # 标题也汉化
        notify.push_message('AnyRouter 签到通知', notify_content, msg_type='text')
        print('[通知] 通知已发送')
    else:
        print('[信息] 无需发送通知 (这行代码理论上不会执行到)')

    # 设置退出码
    sys.exit(0 if success_count > 0 else 1)


def run_main():
    """运行主函数的包装函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[警告] 程序被用户中断')
        sys.exit(1)
    except Exception as e:
        print(f'\n[失败] 程序执行期间发生错误: {e}')
        sys.exit(1)


if __name__ == '__main__':
    run_main()
