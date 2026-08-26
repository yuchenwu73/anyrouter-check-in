#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
	sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
	sys.stderr.reconfigure(line_buffering=True)

import httpx
from cloakbrowser import launch_async
from dotenv import load_dotenv

from utils.browser import (
	BrowserLoginResult,
	has_session_cookie,
	is_logged_in,
	launch_login_context,
	load_browser_login_settings,
	login_with_email_form,
	navigate_login_page,
	prepare_browser_page,
	save_login_screenshot,
	take_pending_screenshots,
	verify_browser_login,
	wait_for_waf_ready,
)
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.debug import debug_print, is_debug_enabled
from utils.notify import notify
from utils.proxy import get_playwright_proxy, get_proxy_server

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'
# 今日到账基线：跨运行记住每个账号今天有没有真的拿到额度
CHECK_IN_STATE_FILE = 'checkin_state.json'

# 签到接口响应体日志截断长度
CHECK_IN_BODY_LOG_LIMIT = int(os.getenv('CHECKIN_BODY_LOG_LIMIT', '300'))
# 额度入账可能滞后于签到请求，签到后按此配置轮询复读余额
CHECK_IN_SETTLE_ATTEMPTS = int(os.getenv('CHECKIN_SETTLE_ATTEMPTS', '3'))
CHECK_IN_SETTLE_DELAY_S = float(os.getenv('CHECKIN_SETTLE_DELAY_S', '3'))
# agentrouter 对同 IP 连续请求会限流（返回 WAF 页而非 JSON），冷却后重试
AUTO_CHECKIN_RETRY_ATTEMPTS = int(os.getenv('CHECKIN_AUTO_RETRY_ATTEMPTS', '3'))
AUTO_CHECKIN_RETRY_DELAY_S = float(os.getenv('CHECKIN_AUTO_RETRY_DELAY_S', '20'))
# 取 WAF cookies 要用浏览器打开登录页，慢节点会超时，失败后换节点重试
WAF_COOKIE_ATTEMPTS = int(os.getenv('CHECKIN_WAF_COOKIE_ATTEMPTS', '3'))
# 平台明令禁止自动化刷量，而签到额度一天只发一次，多跑的请求纯属白增封号风险。
# 所以确认到账后当天就不再碰这个账号；没到账时也限次数，留一次容错就够
DAILY_ATTEMPT_LIMIT = int(os.getenv('CHECKIN_DAILY_ATTEMPT_LIMIT', '2'))

# mihomo Clash API 地址（setup_mihomo_proxy.sh 写入），设置后每个走代理的账号轮换出口节点
PROXY_CONTROLLER = os.getenv('CHECKIN_PROXY_CONTROLLER', '').strip()
# 机场订阅里混着「剩余流量：17 GB」这类信息占位节点，多半连不通，轮换时跳过
PROXY_INFO_NODE_KEYWORDS = ('剩余流量', '套餐到期', '距离下次重置', '官网', '过期时间')
_proxy_node_cursor = 0


def rotate_proxy_node(account_name: str) -> None:
	"""通过 Clash API 把 CHECKIN 组切到下一个出口节点，让每个账号用不同 IP（规避同 IP 限流）"""
	global _proxy_node_cursor
	if not PROXY_CONTROLLER:
		return
	proxy_url = os.getenv('CHECKIN_PROXY_URL', '').strip()
	try:
		with httpx.Client(timeout=10) as client:
			info = client.get(f'{PROXY_CONTROLLER}/proxies/CHECKIN').json()
			nodes = [
				n
				for n in info.get('all', [])
				if n != 'AUTO' and not any(kw in n for kw in PROXY_INFO_NODE_KEYWORDS)
			]
			if not nodes:
				return
			# 最多探测 5 个节点，坏节点跳过
			for _ in range(min(len(nodes), 5)):
				target = nodes[_proxy_node_cursor % len(nodes)]
				_proxy_node_cursor += 1
				client.put(f'{PROXY_CONTROLLER}/proxies/CHECKIN', json={'name': target})
				if not proxy_url:
					print(f'[PROXY] {account_name}: exit node -> {target} (unverified)')
					return
				try:
					with httpx.Client(proxy=proxy_url, timeout=10) as probe:
						resp = probe.get('https://www.gstatic.com/generate_204')
					if resp.status_code in (200, 204):
						print(f'[PROXY] {account_name}: exit node -> {target}')
						return
				except Exception:
					pass
				print(f'[PROXY] {account_name}: node "{target}" unreachable, trying next')
			# 全部探测失败则回退自动选择
			client.put(f'{PROXY_CONTROLLER}/proxies/CHECKIN', json={'name': 'AUTO'})
			print(f'[PROXY] {account_name}: all probed nodes failed, fallback to AUTO')
	except Exception as e:
		print(f'[WARN] {account_name}: proxy node rotation failed: {str(e)[:80]}')


def load_balance_hash():
	"""加载余额hash"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
				return f.read().strip()
	except Exception:  # nosec B110
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
	simple_balances = (
		{k: {'quota': v.get('quota'), 'used': v.get('used')} for k, v in balances.items()} if balances else {}
	)
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def today_key() -> str:
	"""今日日期，CI 里由 TZ=Asia/Shanghai 决定"""
	return datetime.now().strftime('%Y-%m-%d')


def load_daily_state() -> dict:
	"""加载今日到账基线，跨天自动重置

	单次运行的余额 delta 说明不了「今天签没签上」：额度可能是更早那次运行拿到的，
	也可能是在两次运行的间隙里到账的（邮箱密码登录会先跑一遍浏览器登录，
	等脚本读「签到前」余额时钱已经进来了）。所以这里既存当日奖励，也存余额基线。
	"""
	state: dict = {'date': today_key(), 'accounts': {}}
	try:
		if os.path.exists(CHECK_IN_STATE_FILE):
			with open(CHECK_IN_STATE_FILE, 'r', encoding='utf-8') as f:
				saved = json.load(f)
			if isinstance(saved, dict) and isinstance(saved.get('accounts'), dict):
				if saved.get('date') == state['date']:
					state['accounts'] = saved['accounts']
					credited = sum(1 for r in state['accounts'].values() if r.get('reward', 0) > 0.01)
					print(f'[STATE] Daily baseline loaded: {credited} account(s) credited today')
				else:
					# 跨天只清零当日奖励和尝试次数，余额基线要留着，否则认不出间隙里到账的额度
					state['accounts'] = {
						name: {k: rec[k] for k in ('max_total', 'quota', 'used') if k in rec}
						for name, rec in saved['accounts'].items()
						if isinstance(rec, dict) and 'max_total' in rec
					}
					print(f'[STATE] New day {state["date"]} (was {saved.get("date")}), daily rewards reset')
	except Exception as e:
		print(f'[WARN] Failed to load daily state: {e}')
	return state


def save_daily_state(state: dict) -> None:
	"""保存今日到账基线"""
	try:
		with open(CHECK_IN_STATE_FILE, 'w', encoding='utf-8') as f:
			json.dump(state, f, ensure_ascii=False, sort_keys=True)
	except Exception as e:
		print(f'[WARN] Failed to save daily state: {e}')


def record_daily_reward(state: dict, account_name: str, reward: float) -> dict:
	"""把到账额度累加进今日基线，观测时间保留最早一次"""
	record: dict = state['accounts'].setdefault(account_name, {})
	record['reward'] = round(record.get('reward', 0.0) + reward, 2)
	record['at'] = record.get('at') or datetime.now().strftime('%H:%M:%S')
	return record


def observe_balance(state: dict, account_name: str, totals: list[float]) -> float:
	"""对比余额基线，返回本次新观测到的到账额度，同时抬高基线

	基线取「观测到的最大总额」而不是上次运行的读数。总额（余额 + 累计消耗）只会
	因为到账而上升，但账号正在用的时候接口可能先扣余额、后记消耗，中间态会让总额
	短暂偏低；拿上次读数当基线，就会把这个恢复过程误判成一笔到账。
	"""
	observed = max(totals)
	record: dict = state['accounts'].setdefault(account_name, {})
	baseline = record.get('max_total')

	if baseline is None:
		record['max_total'] = round(observed, 2)
		return 0.0

	# 所有读数都明显低于基线，说明额度真被下调过，基线跟着降，否则以后再也认不出到账
	if observed < baseline - 1:
		print(f'[STATE] {account_name}: baseline lowered ${baseline:.2f} -> ${observed:.2f}')
		record['max_total'] = round(observed, 2)
		return 0.0

	credited = observed - baseline
	if credited > 0.01:
		record['max_total'] = round(observed, 2)
		return credited
	return 0.0


def skip_reason_today(record: dict, provider_config, now_hour: int) -> str | None:
	"""当天是否该跳过这个账号，返回跳过原因；None 表示照常处理

	平台把「自动化刷量」写进了封禁条款，而签到额度一天只发一次：钱到手之后再登录，
	既拿不到东西，又多留一条自动化痕迹。所以能不发请求就不发，把频率压到和手动一致。
	"""
	if record.get('reward', 0) > 0.01:
		return '今日额度已到账'

	reset_hour = provider_config.checkin_reset_hour
	if now_hour < reset_hour:
		return f'额度每天 {reset_hour} 点后才刷新'

	attempts = record.get('attempts', 0)
	if attempts >= DAILY_ATTEMPT_LIMIT:
		return f'当日已尝试 {attempts} 次，未到账也不再重试'

	return None


def remember_balance(record: dict, quota: float, used: float) -> None:
	"""记下最近一次读到的余额，供当天后续跳过时填通知"""
	record['quota'] = round(quota, 2)
	record['used'] = round(used, 2)


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


async def get_waf_cookies_with_browser(
	account_name: str,
	login_url: str,
	required_cookies: list[str],
	*,
	use_proxy: bool = False,
):
	"""使用浏览器获取 WAF cookies"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	launch_kwargs: dict = {'headless': True}
	proxy = get_playwright_proxy(use_proxy=use_proxy)
	if proxy:
		launch_kwargs['proxy'] = proxy
	browser = await launch_async(**launch_kwargs)

	try:
		page = await browser.new_page()
		await prepare_browser_page(page)
		print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')

		await page.goto(login_url, wait_until='domcontentloaded')
		await wait_for_waf_ready(page)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name in required_cookies and cookie_value is not None:
				waf_cookies[cookie_name] = cookie_value

		print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

		missing_cookies = [c for c in required_cookies if c not in waf_cookies]

		if missing_cookies:
			print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
			await browser.close()
			return None

		print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')
		await browser.close()
		return waf_cookies

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {e}')
		await browser.close()
		return None


async def login_with_credentials(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
	*,
	use_proxy_override: bool | None = None,
) -> BrowserLoginResult | None:
	"""使用邮箱密码通过浏览器登录，返回 cookies 与拦截到的 api user id。"""
	print(f'[PROCESSING] {account_name}: Logging in with email/password...')

	use_proxy = provider_config.use_proxy if use_proxy_override is None else use_proxy_override
	login_url = f'{provider_config.domain}{provider_config.login_path}'
	settings = load_browser_login_settings(
		account_name,
		provider_name,
		persist_profile=provider_config.persist_profile,
	)
	timeout_ms = settings.wait_timeout_ms

	debug_print(
		f'[INFO] {account_name}: Browser profile={settings.profile_dir}, '
		f'persist={settings.persist_profile}, headless={settings.headless}, '
		f'humanize={settings.humanize}, timeout={timeout_ms}ms'
	)

	print(f'[INFO] {account_name}: Provider proxy={"enabled" if use_proxy else "disabled"} ({provider_name})')

	try:
		context = await launch_login_context(settings, use_proxy=use_proxy)
	except Exception as e:
		print(f'[FAILED] {account_name}: Browser launch failed: {e}')
		return None

	page = None
	try:
		page = await context.new_page()
		await prepare_browser_page(page)
		await navigate_login_page(
			page,
			login_url,
			timeout_ms,
			provider=provider_name,
			account_name=account_name,
		)

		if not await is_logged_in(page):
			if await has_session_cookie(page):
				print(f'[WARN] {account_name}: Stale session cookie on login page, forcing email login')
			await save_login_screenshot(page, provider_name, account_name, 'before-email-login')
			await login_with_email_form(
				page,
				email,
				password,
				timeout_ms,
				provider=provider_name,
				account_name=account_name,
			)
		else:
			print(f'[INFO] {account_name}: Browser profile already logged in')

		console_url = f'{provider_config.domain}/console'
		user_profile = await verify_browser_login(page, console_url, timeout_ms)
		if not user_profile:
			cookies = await context.cookies()
			cookie_names = [c.get('name') for c in cookies if c.get('name')]
			print(f'[FAILED] {account_name}: Login failed - /api/user/self not verified')
			debug_print(f'[INFO] {account_name}: Current URL: {page.url}')
			debug_print(f'[INFO] {account_name}: Got cookies: {cookie_names}')
			await save_login_screenshot(page, provider_name, account_name, 'not-authenticated')
			await context.close()
			return None

		cookies = await context.cookies()
		all_cookies = {
			cookie.get('name'): cookie.get('value') for cookie in cookies if cookie.get('name') and cookie.get('value')
		}
		api_user = str(user_profile['id']) if user_profile.get('id') is not None else None

		success_msg = f'[SUCCESS] {account_name}: Login successful, got {len(all_cookies)} cookies'
		if is_debug_enabled() and api_user:
			success_msg += f', api_user={api_user}'
		print(success_msg)
		await context.close()
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user)

	except Exception as e:
		print(f'[FAILED] {account_name}: Error during login: {e}')
		if page is not None:
			await save_login_screenshot(page, provider_name, account_name, 'login-error')
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
					'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
				}
		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


def quota_total(user_info) -> float | None:
	"""签到前后对比用的总额（余额 + 累计消耗），读不到返回 None"""
	if user_info and user_info.get('success'):
		return float(user_info['quota']) + float(user_info['used_quota'])
	return None


def get_user_info_after_check_in(client, headers, user_info_url: str, account_name: str, before_total):
	"""签到后读取用户信息

	额度入账可能滞后于签到请求，立即复读会拿到旧值并被误判成「无奖励」。
	这里轮询到总额上升为止；只等上升不等下降，因为账号在用的时候接口会先扣余额、
	后记消耗，总额的短暂回落不是签到结果。全程没涨也照常返回最后一次读数。
	"""
	user_info = None
	for attempt in range(1, CHECK_IN_SETTLE_ATTEMPTS + 1):
		if CHECK_IN_SETTLE_DELAY_S > 0:
			time.sleep(CHECK_IN_SETTLE_DELAY_S)
		user_info = get_user_info(client, headers, user_info_url)
		after_total = quota_total(user_info)

		if after_total is None:
			print(f'[SETTLE] {account_name}: attempt {attempt}/{CHECK_IN_SETTLE_ATTEMPTS} user info unavailable')
			continue

		if before_total is None or after_total - before_total > 0.01:
			print(f'[SETTLE] {account_name}: settled on attempt {attempt} (total ${after_total:.2f})')
			return user_info

		print(
			f'[SETTLE] {account_name}: attempt {attempt}/{CHECK_IN_SETTLE_ATTEMPTS} '
			f'no rise yet (total ${after_total:.2f})'
		)

	return user_info


async def prepare_cookies(
	account_name: str, provider_config, user_cookies: dict, *, use_proxy: bool | None = None
) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}
	use_proxy = provider_config.use_proxy if use_proxy is None else use_proxy

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		# 机场节点可能通得过连通性探测却慢到打不开页面，失败就换个节点重来
		for attempt in range(1, WAF_COOKIE_ATTEMPTS + 1):
			waf_cookies = await get_waf_cookies_with_browser(
				account_name,
				login_url,
				provider_config.waf_cookie_names,
				use_proxy=use_proxy,
			)
			if waf_cookies:
				break
			if attempt < WAF_COOKIE_ATTEMPTS:
				print(f'[RETRY] {account_name}: WAF cookies attempt {attempt}/{WAF_COOKIE_ATTEMPTS} failed, switching node')
				rotate_proxy_node(account_name)
		if not waf_cookies:
			print(f'[FAILED] {account_name}: Unable to get WAF cookies')
			return None
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict):
	"""执行签到请求"""
	print(f'[NETWORK] {account_name}: Executing check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')
	# 诊断用：接口对「首次签到」和「今天已签过」可能返回同样的成功码，只有 body 能区分
	body_preview = ' '.join(response.text.split())[:CHECK_IN_BODY_LOG_LIMIT]
	print(f'[RESPONSE-BODY] {account_name}: {body_preview}')

	if response.status_code == 200:
		try:
			result = response.json()
			if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
				print(f'[SUCCESS] {account_name}: Check-in successful!')
				return True
			else:
				error_msg = result.get('msg', result.get('message', 'Unknown error'))
				already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
				if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
					print(f'[SUCCESS] {account_name}: Already checked in today')
					return True
				print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
				return False
		except json.JSONDecodeError:
			if 'success' in response.text.lower():
				print(f'[SUCCESS] {account_name}: Check-in successful!')
				return True
			else:
				print(f'[FAILED] {account_name}: Check-in failed - Invalid response format')
				return False
	else:
		print(f'[FAILED] {account_name}: Check-in failed - HTTP {response.status_code}')
		return False


def format_check_in_notification(detail: dict, today_record: dict | None = None, credited_this_run: bool = False) -> str:
	"""格式化签到通知消息"""
	if detail.get('skipped'):
		# 本次没发请求（风控），余额是当天早先记下的读数，不是实时值
		reward = (today_record or {}).get('reward', 0.0)
		landed_at = (today_record or {}).get('at')
		when = f'（{landed_at} 观测到）' if landed_at else ''
		if reward > 0.01:
			tail = f'  今日额度已到账 +${reward:.2f}{when}，当日不再重复登录'
		else:
			tail = f'  本次跳过: {detail["skipped"]}'
		return '\n'.join(
			[
				f'[CHECK-IN] {detail["name"]}',
				'  ━━━━━━━━━━━━━━━━━━━━',
				f'     余额: ${detail["after_quota"]:.2f}  |  累计消耗: ${detail["after_used"]:.2f}（当日记录值）',
				'  ━━━━━━━━━━━━━━━━━━━━',
				tail,
			]
		)

	lines = [
		f'[CHECK-IN] {detail["name"]}',
		'  ━━━━━━━━━━━━━━━━━━━━',
		'  签到前',
		f'     余额: ${detail["before_quota"]:.2f}  |  累计消耗: ${detail["before_used"]:.2f}',
		'  签到后',
		f'     余额: ${detail["after_quota"]:.2f}  |  累计消耗: ${detail["after_used"]:.2f}',
	]

	# 阈值和 main() 里记账用的一致：余额取整到分，跨读数会有 ±0.01 抖动
	has_reward = detail['check_in_reward'] > 0.01
	has_usage = detail['usage_increase'] != 0

	lines.append('  ━━━━━━━━━━━━━━━━━━━━')

	if has_reward:
		lines.append(f'  签到获得: +${detail["check_in_reward"]:.2f}')
	elif today_record and today_record.get('reward', 0) > 0.01:
		landed_at = today_record.get('at')
		when = f'（{landed_at} 观测到）' if landed_at else ''
		if credited_this_run:
			# 邮箱密码账号的额度在浏览器登录时就发放了，读「签到前」余额时已经进账
			lines.append(f'  签到获得: +${today_record["reward"]:.2f}{when}，登录时已到账')
		else:
			# 本次没到账，但今天早些时候已经到账了，别写成像失败的样子
			lines.append(f'  今日额度已到账 +${today_record["reward"]:.2f}{when}，本次运行未再到账')
	else:
		# 今天从没观测到额度到账，这才是需要留意的情况
		lines.append('  今日尚未观测到额度到账')

	if has_usage:
		lines.append(f'  期间消耗: ${detail["usage_increase"]:.2f}')

	if detail['balance_change'] != 0:
		change_symbol = '+' if detail['balance_change'] > 0 else ''
		lines.append(f'  余额变化: {change_symbol}${detail["balance_change"]:.2f}')

	return '\n'.join(lines)


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
	"""为单个账号执行签到操作"""
	account_name = account.get_display_name(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
		return False, None, None

	print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

	# 账号级 use_proxy 优先：被 WAF 按「账号 + 出口 IP」拉黑的号直接走代理，不必先撞一次 403
	use_proxy = account.resolve_use_proxy(provider_config.use_proxy)
	if use_proxy != provider_config.use_proxy:
		print(f'[INFO] {account_name}: Proxy forced by account config -> {"enabled" if use_proxy else "disabled"}')

	# 走代理的账号先轮换出口节点，避免多账号同 IP 连续请求被限流
	if use_proxy:
		rotate_proxy_node(account_name)

	# 邮箱密码优先
	all_cookies = None
	resolved_api_user: str | None = None
	auth_method = None
	if account.has_login_credentials():
		print(f'[INFO] {account_name}: Attempting email/password login (priority)...')
		assert account.email is not None and account.password is not None
		login_result = await login_with_credentials(
			account_name,
			provider_config,
			account.provider,
			account.email,
			account.password,
			use_proxy_override=use_proxy,
		)
		if login_result:
			all_cookies = login_result.cookies
			resolved_api_user = login_result.api_user
			auth_method = 'email/password'
		else:
			print(f'[FAILED] {account_name}: Email/password login failed, will not use stale session cookies')
			return False, None, None
	else:
		user_cookies = parse_cookies(account.cookies)
		if not user_cookies:
			print(f'[FAILED] {account_name}: Invalid configuration format')
			return False, None, None
		all_cookies = await prepare_cookies(account_name, provider_config, user_cookies, use_proxy=use_proxy)
		auth_method = 'session cookies'

	if not all_cookies:
		return False, None, None

	print(f'[AUTH] {account_name}: Using auth method -> {auth_method}')

	result = run_check_in_requests(
		all_cookies,
		account,
		account_name,
		provider_config,
		api_user_override=resolved_api_user,
		use_proxy=use_proxy,
	)

	# 已经在代理上还被 403 就没别的招了，只有直连账号才值得清 profile 换代理重来
	if not result[0] and not use_proxy and account.has_login_credentials() and hit_waf_403(result[1], result[2]):
		assert account.email is not None and account.password is not None
		print(f'[RETRY] {account_name}: HTTP 403, wiping browser profile and retrying via proxy')
		settings = load_browser_login_settings(
			account_name,
			account.provider,
			persist_profile=provider_config.persist_profile,
		)
		shutil.rmtree(settings.profile_dir, ignore_errors=True)
		# 换出口 IP：runner 机房 IP 已被 WAF 标记，走机场节点重新登录
		rotate_proxy_node(account_name)
		login_result = await login_with_credentials(
			account_name,
			provider_config,
			account.provider,
			account.email,
			account.password,
			use_proxy_override=True,
		)
		if login_result:
			result = run_check_in_requests(
				login_result.cookies,
				account,
				account_name,
				provider_config,
				api_user_override=login_result.api_user,
				use_proxy=True,
			)

	return result


def hit_waf_403(*infos) -> bool:
	"""用户信息读取结果里是否出现 HTTP 403（WAF 拦截特征）"""
	for info in infos:
		if info and 'HTTP 403' in str(info.get('error', '')):
			return True
	return False


def run_check_in_requests(
	all_cookies: dict,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
	use_proxy: bool = False,
) -> tuple[bool, dict | None, dict | None]:
	"""执行 HTTP 签到请求（同步，避免在 async 上下文中使用阻塞 httpx）。"""
	try:
		client_kwargs: dict = {'http2': True, 'timeout': 30.0}
		proxy_url = get_proxy_server(use_proxy=use_proxy)
		if proxy_url:
			client_kwargs['proxy'] = proxy_url
			if is_debug_enabled():
				print(f'[INFO] {account_name}: HTTP client proxy enabled: {proxy_url}')
			else:
				print(f'[INFO] {account_name}: HTTP client proxy enabled')
		elif use_proxy:
			print(f'[WARN] {account_name}: Provider requires proxy but CHECKIN_PROXY_URL is not set')

		with httpx.Client(**client_kwargs) as client:
			client.cookies.update(all_cookies)

			headers = {
				'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				'Accept': 'application/json, text/plain, */*',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br, zstd',
				'Referer': provider_config.domain,
				'Origin': provider_config.domain,
				'Connection': 'keep-alive',
				'Sec-Fetch-Dest': 'empty',
				'Sec-Fetch-Mode': 'cors',
				'Sec-Fetch-Site': 'same-origin',
			}

			api_user = api_user_override or account.api_user
			if api_user:
				headers[provider_config.api_user_key] = api_user

			user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
			user_info_before = get_user_info(client, headers, user_info_url)
			if user_info_before and user_info_before.get('success'):
				print(user_info_before['display'])
			elif user_info_before:
				print(user_info_before.get('error', 'Unknown error'))

			if provider_config.needs_manual_check_in():
				success = execute_check_in(client, account_name, provider_config, headers)
				user_info_after = get_user_info_after_check_in(
					client, headers, user_info_url, account_name, quota_total(user_info_before)
				)
				return success, user_info_before, user_info_after

			user_info_after = get_user_info(client, headers, user_info_url)
			attempt = 1
			while not (user_info_after and user_info_after.get('success')) and attempt < AUTO_CHECKIN_RETRY_ATTEMPTS:
				attempt += 1
				print(
					f'[RETRY] {account_name}: user info blocked (rate limit?), '
					f'cooling down {AUTO_CHECKIN_RETRY_DELAY_S:.0f}s before attempt {attempt}/{AUTO_CHECKIN_RETRY_ATTEMPTS}'
				)
				time.sleep(AUTO_CHECKIN_RETRY_DELAY_S)
				user_info_after = get_user_info(client, headers, user_info_url)
			if user_info_after and user_info_after.get('success'):
				print(f'[INFO] {account_name}: Check-in completed automatically (triggered by user info request)')
				return True, user_info_before, user_info_after
			error = user_info_after.get('error', 'Unknown error') if user_info_after else 'Unknown error'
			print(f'[FAILED] {account_name}: Auto check-in failed - {error}')
			return False, user_info_before, user_info_after

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred during check-in process - {str(e)[:50]}...')
		return False, None, None


async def main():
	"""主函数"""
	if is_debug_enabled():
		print('[INFO] DEBUG_MODE enabled')
		proxy_server = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if proxy_server:
			print(f'[INFO] Proxy endpoint available: {proxy_server} (enabled per provider use_proxy)')
		else:
			print('[INFO] CHECKIN_PROXY_URL not set; providers with use_proxy=true will run without proxy')
	else:
		print('[INFO] Debug mode disabled (set DEBUG_MODE=true to enable screenshots and verbose logs)')

	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started')
	print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')
	if is_debug_enabled():
		for provider_name, provider in sorted(app_config.providers.items()):
			print(f'[INFO] Provider "{provider_name}": use_proxy={provider.use_proxy}')

	accounts = load_accounts_config()
	if not accounts:
		error_msg = '[FAILED] Unable to load account configuration, program exits'
		print(error_msg)
		notify.push_message('AnyRouter Check-in Alert', error_msg, msg_type='text')
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	daily_state = load_daily_state()
	# 本次运行观测到的到账，键为账号名。间隙到账和当场到账都要记进来，通知开关看它
	credited_this_run: dict = {}

	success_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	account_check_in_details = {}
	need_notify = False
	should_report_details = False

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		display_name = account.get_display_name(i)
		record = daily_state['accounts'].setdefault(display_name, {})

		# 风控：今天已到账、额度还没刷新、当日次数用满，三种情况都不再发请求
		provider_config = app_config.get_provider(account.provider)
		skip = skip_reason_today(record, provider_config, datetime.now().hour) if provider_config else None
		if skip:
			print(f'\n[SKIP] {display_name}: {skip}，本次不发请求')
			success_count += 1
			if 'quota' in record and 'used' in record:
				current_balances[account_key] = {'quota': record['quota'], 'used': record['used']}
				account_check_in_details[account_key] = {
					'name': display_name,
					'before_quota': record['quota'],
					'before_used': record['used'],
					'after_quota': record['quota'],
					'after_used': record['used'],
					'check_in_reward': 0.0,
					'usage_increase': 0,
					'balance_change': 0,
					'success': True,
					'skipped': skip,
				}
			continue

		# 尝试次数在发请求前就记上，中途异常退出也不会让当天次数白涨
		record['attempts'] = record.get('attempts', 0) + 1
		try:
			success, user_info_before, user_info_after = await check_in_account(account, i, app_config)
			if success:
				success_count += 1

			should_notify_this_account = False

			if not success:
				should_notify_this_account = True
				need_notify = True
				account_name = account.get_display_name(i)
				print(f'[NOTIFY] {account_name} failed, will send notification')

			if user_info_after and user_info_after.get('success'):
				current_quota = user_info_after['quota']
				current_used = user_info_after['used_quota']
				current_balances[account_key] = {'quota': current_quota, 'used': current_used}

				if user_info_before and user_info_before.get('success'):
					before_quota = user_info_before['quota']
					before_used = user_info_before['used_quota']
					after_quota = user_info_after['quota']
					after_used = user_info_after['used_quota']

					total_before = before_quota + before_used
					total_after = after_quota + after_used

					check_in_reward = total_after - total_before
					usage_increase = after_used - before_used
					balance_change = after_quota - before_quota

					account_check_in_details[account_key] = {
						'name': account.get_display_name(i),
						'before_quota': before_quota,
						'before_used': before_used,
						'after_quota': after_quota,
						'after_used': after_used,
						'check_in_reward': check_in_reward,
						'usage_increase': usage_increase,
						'balance_change': balance_change,
						'success': success,
					}

					# 记下余额，当天后续运行跳过时拿它填通知
					remember_balance(record, after_quota, after_used)

					# 到账既可能发生在本次运行内，也可能在上次运行之后（登录动作就会触发签到），
					# 两种都表现为总额高过基线，所以拿本次两次读数一起跟基线比
					credited = observe_balance(daily_state, display_name, [total_before, total_after])
					if credited > 0.01:
						record_daily_reward(daily_state, display_name, credited)
						credited_this_run[display_name] = credited_this_run.get(display_name, 0.0) + credited
						print(f'[STATE] {display_name}: +${credited:.2f} credited today')

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				status = '[SUCCESS]' if success else '[FAIL]'
				account_result = f'{status} {account_name}'
				if user_info_after and user_info_after.get('success'):
					account_result += f'\n{user_info_after["display"]}'
				elif user_info_after:
					account_result += f'\n{user_info_after.get("error", "Unknown error")}'
				notification_content.append(account_result)

		except Exception as e:
			account_name = account.get_display_name(i)
			print(f'[FAILED] {account_name} processing exception: {e}')
			need_notify = True
			notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')

	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None

	# 本次观测到额度到账才通知：可能是签到当场拿到的，也可能是上次运行之后进来的。
	# 平时用掉额度导致的余额下降不算——总额（余额 + 累计消耗）对消耗是不变的
	if credited_this_run:
		should_report_details = True
		need_notify = True
		total_credited = sum(credited_this_run.values())
		print(f'[NOTIFY] ${total_credited:.2f} credited across {len(credited_this_run)} account(s), will notify')
	else:
		print('[INFO] No credit observed this run, balance notification skipped')

	if should_report_details:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in account_check_in_details:
				detail = account_check_in_details[account_key]
				account_name = detail['name']
				account_result = format_check_in_notification(
					detail,
					daily_state['accounts'].get(account_name),
					credited_this_run=account_name in credited_this_run,
				)
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	save_daily_state(daily_state)

	if need_notify and notification_content:
		summary = [
			'[STATS] Check-in result statistics:',
			f'[SUCCESS] Success: {success_count}/{total_count}',
			f'[FAIL] Failed: {total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('[SUCCESS] All accounts check-in successful!')
		elif success_count > 0:
			summary.append('[WARN] Some accounts check-in successful')
		else:
			summary.append('[ERROR] All accounts check-in failed')

		time_info = f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])
		screenshot_paths = take_pending_screenshots() if is_debug_enabled() else []
		if screenshot_paths:
			github_run_id = os.getenv('GITHUB_RUN_ID', '').strip()
			github_repo = os.getenv('GITHUB_REPOSITORY', '').strip()
			screenshot_hint = f'[SCREENSHOT] {len(screenshot_paths)} debug screenshot(s) saved'
			if github_run_id and github_repo:
				run_url = f'https://github.com/{github_repo}/actions/runs/{github_run_id}'
				screenshot_hint += f'. Download artifact `checkin-screenshots-{github_run_id}` from: {run_url}'
			else:
				screenshot_hint += ' to `checkin_screenshots/`'
			notify_content += f'\n\n{screenshot_hint}'

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		print('[NOTIFY] Notification sent due to failures or balance changes')
	else:
		print('[INFO] All accounts successful and no balance changes detected, notification skipped')

	sys.exit(0 if success_count > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
