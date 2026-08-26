import base64
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin
from checkin import cookie_expiry_warnings, session_cookie_days_left
from utils.config import AccountConfig


def make_session(days_ago: float) -> str:
	"""伪造一个 NewAPI 风格的 session cookie：base64("<签发秒>|<载荷>|<签名>")"""
	issued = int(time.time() - days_ago * 86400)
	return base64.b64encode(f'{issued}|cGF5bG9hZA==|sig'.encode()).decode()


def test_days_left_counts_down_from_the_cookie_lifetime():
	assert session_cookie_days_left(make_session(0)) == checkin.SESSION_COOKIE_LIFETIME_DAYS
	assert session_cookie_days_left(make_session(28)) == checkin.SESSION_COOKIE_LIFETIME_DAYS - 28


def test_days_left_goes_negative_once_the_cookie_is_stale():
	assert session_cookie_days_left(make_session(33)) == -3


def test_garbage_is_not_mistaken_for_a_timestamp():
	assert session_cookie_days_left('not-a-cookie') is None
	assert session_cookie_days_left('') is None
	# 能解 base64 但第一段不是数字
	assert session_cookie_days_left(base64.b64encode(b'abc|def|ghi').decode()) is None


def test_absurd_timestamps_are_rejected_rather_than_warned_about():
	# 未来时间戳和上古时间戳都说明没解对格式，误报比不报更糟
	assert session_cookie_days_left(make_session(-5)) is None
	assert session_cookie_days_left(make_session(4000)) is None


def test_a_fresh_cookie_raises_no_warning():
	accounts = [AccountConfig.from_dict({'name': 'L站小号', 'cookies': {'session': make_session(3)}, 'api_user': '1'}, 0)]

	assert cookie_expiry_warnings(accounts) == []


def test_a_cookie_near_its_end_gets_flagged():
	accounts = [AccountConfig.from_dict({'name': 'L站小号', 'cookies': {'session': make_session(27)}, 'api_user': '1'}, 0)]

	warnings = cookie_expiry_warnings(accounts)

	assert len(warnings) == 1
	assert 'L站小号' in warnings[0]
	assert '约剩 3 天' in warnings[0]


def test_an_already_expired_cookie_says_so():
	accounts = [AccountConfig.from_dict({'name': 'L站小号', 'cookies': {'session': make_session(34)}, 'api_user': '1'}, 0)]

	warnings = cookie_expiry_warnings(accounts)

	assert '已超期' in warnings[0]
	assert '401' in warnings[0]


def test_email_password_accounts_are_never_flagged():
	# 每次登录都会换发新 cookie，没有需要人工维护的过期问题
	accounts = [
		AccountConfig.from_dict(
			{'name': 'edu号', 'email': 'a@b.c', 'password': 'x', 'cookies': {'session': make_session(99)}}, 0
		)
	]

	assert cookie_expiry_warnings(accounts) == []


def test_accounts_without_a_session_cookie_are_skipped():
	accounts = [AccountConfig.from_dict({'name': '怪号', 'cookies': {'other': 'v'}, 'api_user': '1'}, 0)]

	assert cookie_expiry_warnings(accounts) == []
