import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin
from checkin import (
	format_check_in_notification,
	generate_balance_hash,
	load_daily_state,
	observe_balance,
	record_daily_reward,
	remember_balance,
	save_daily_state,
	skip_reason_today,
)
from utils.config import ProviderConfig


def make_detail(reward=0.0, usage=0.0):
	"""构造一份签到明细，默认是「本次运行余额没动」"""
	return {
		'name': 'AnyRouter-zjwei',
		'before_quota': 700.37,
		'before_used': 2970.38,
		'after_quota': 700.37 + reward,
		'after_used': 2970.38 + usage,
		'check_in_reward': reward + usage,
		'usage_increase': usage,
		'balance_change': reward,
		'success': True,
	}


def test_balance_hash_changes_when_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 125.0, 'used': 20.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_changes_when_used_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 100.0, 'used': 21.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_is_stable_for_equivalent_balances():
	left = {
		'account_2': {'quota': 50.0, 'used': 1.0},
		'account_1': {'quota': 100.0, 'used': 20.0},
	}
	right = {
		'account_1': {'used': 20.0, 'quota': 100.0},
		'account_2': {'used': 1.0, 'quota': 50.0},
	}

	assert generate_balance_hash(left) == generate_balance_hash(right)


def test_daily_state_survives_within_the_same_day(tmp_path, monkeypatch):
	state_file = tmp_path / 'checkin_state.json'
	monkeypatch.setattr(checkin, 'CHECK_IN_STATE_FILE', str(state_file))

	state = load_daily_state()
	record_daily_reward(state, 'AnyRouter-zjwei', 25.0)
	save_daily_state(state)

	reloaded = load_daily_state()
	assert reloaded['accounts']['AnyRouter-zjwei']['reward'] == 25.0


def test_daily_state_resets_on_a_new_day(tmp_path, monkeypatch):
	state_file = tmp_path / 'checkin_state.json'
	monkeypatch.setattr(checkin, 'CHECK_IN_STATE_FILE', str(state_file))
	stale = {
		'date': '2000-01-01',
		'accounts': {'AnyRouter-zjwei': {'reward': 25.0, 'at': '02:21:31', 'max_total': 3645.75}},
	}
	state_file.write_text(json.dumps(stale), encoding='utf-8')

	accounts = load_daily_state()['accounts']

	# 当日奖励清零，但余额基线要留着，否则认不出间隙里到账的额度
	assert accounts['AnyRouter-zjwei'] == {'max_total': 3645.75}


def test_first_observation_only_seeds_the_baseline():
	state = {'date': '2026-08-16', 'accounts': {}}

	assert observe_balance(state, 'AnyRouter-zjwei', [3645.75, 3645.75]) == 0.0
	assert state['accounts']['AnyRouter-zjwei']['max_total'] == 3645.75


def test_observe_balance_detects_credit_landing_between_runs():
	# 02:20 跑完基线 3645.75；08:59 再跑时「签到前」就已经多了 $25，本次运行内零变化
	state = {'date': '2026-08-16', 'accounts': {'AnyRouter-zjwei': {'max_total': 3645.75}}}

	assert observe_balance(state, 'AnyRouter-zjwei', [3670.75, 3670.75]) == 25.0


def test_observe_balance_detects_credit_landing_within_the_run():
	state = {'date': '2026-08-16', 'accounts': {'L站-小号': {'max_total': 2075.0}}}

	assert observe_balance(state, 'L站-小号', [2075.0, 2100.0]) == 25.0


def test_observe_balance_ignores_a_dip_that_recovers():
	# 账号在用时接口会先扣余额、后记消耗，总额短暂偏低后恢复，不能算成到账
	state = {'date': '2026-08-16', 'accounts': {'2021303397@aust.edu.cn': {'max_total': 2300.0}}}

	assert observe_balance(state, '2021303397@aust.edu.cn', [2284.89, 2300.0]) == 0.0
	assert state['accounts']['2021303397@aust.edu.cn']['max_total'] == 2300.0


def test_observe_balance_lowers_the_baseline_when_quota_is_really_cut():
	# 额度真被下调时基线要跟着降，否则以后再也认不出到账
	state = {'date': '2026-08-16', 'accounts': {'AnyRouter-zjwei': {'max_total': 3670.75}}}

	assert observe_balance(state, 'AnyRouter-zjwei', [100.0, 100.0]) == 0.0
	assert state['accounts']['AnyRouter-zjwei']['max_total'] == 100.0


def test_observe_balance_does_not_credit_twice_for_the_same_rise():
	state = {'date': '2026-08-16', 'accounts': {'AnyRouter-zjwei': {'max_total': 3645.75}}}

	assert observe_balance(state, 'AnyRouter-zjwei', [3670.75, 3670.75]) == 25.0
	assert observe_balance(state, 'AnyRouter-zjwei', [3670.75, 3670.75]) == 0.0


def test_record_daily_reward_accumulates_and_keeps_first_landing_time():
	state = {'date': '2026-08-16', 'accounts': {}}

	first = record_daily_reward(state, 'AnyRouter-zjwei', 25.0)
	second = record_daily_reward(state, 'AnyRouter-zjwei', 25.0)

	assert second['reward'] == 50.0
	assert second['at'] == first['at']


def test_notification_reports_reward_earned_in_this_run():
	message = format_check_in_notification(make_detail(reward=25.0), None)

	assert '签到获得: +$25.00' in message


def test_notification_uses_today_baseline_when_this_run_had_no_change():
	record = {'reward': 25.0, 'at': '02:21:31'}

	message = format_check_in_notification(make_detail(), record)

	assert '今日额度已到账 +$25.00（02:21:31 观测到），本次运行未再到账' in message
	assert '尚未观测到' not in message


def test_notification_credits_login_time_landing_to_this_run():
	# 邮箱密码账号：额度在浏览器登录时就发放，读「签到前」余额时已进账，签到前后差值为 0
	record = {'reward': 25.0, 'at': '08:57:24'}

	message = format_check_in_notification(make_detail(), record, credited_this_run=True)

	assert '签到获得: +$25.00（08:57:24 观测到），登录时已到账' in message
	assert '本次运行未再到账' not in message


def test_notification_does_not_claim_no_change_while_showing_usage():
	# settle 等待期间正好在消耗额度，措辞不能一边说无变化一边列出消耗
	message = format_check_in_notification(make_detail(usage=12.55), {'reward': 25.0, 'at': '02:21:31'})

	assert '期间消耗: $12.55' in message
	assert '无变化' not in message


def test_notification_flags_accounts_with_no_credit_today():
	message = format_check_in_notification(make_detail(), None)

	assert '今日尚未观测到额度到账' in message


def test_notification_flags_accounts_carrying_only_a_balance_baseline():
	# 只有余额基线、今天还没到账的账号，不能被当成已签到
	message = format_check_in_notification(make_detail(), {'max_total': 3645.75})

	assert '今日尚未观测到额度到账' in message


def test_notification_ignores_rounding_jitter_as_reward():
	message = format_check_in_notification(make_detail(reward=0.01), {'reward': 25.0, 'at': '02:21:31'})

	assert '签到获得' not in message
	assert '今日额度已到账 +$25.00' in message
def make_provider(reset_hour=0):
	"""构造一个只关心「额度几点刷新」的 provider"""
	return ProviderConfig(name='test', domain='https://example.com', checkin_reset_hour=reset_hour)


def test_skip_account_that_already_got_credited_today():
	# 钱已经到手，再登录只会多留一条自动化痕迹
	reason = skip_reason_today({'reward': 25.0}, make_provider(), now_hour=9)

	assert reason == '今日额度已到账'


def test_skip_before_the_platform_refreshes_quota():
	# anyrouter 早 8 点才刷新，3 点登录也拿不到钱
	reason = skip_reason_today({}, make_provider(reset_hour=8), now_hour=3)

	assert reason is not None
	assert '8 点' in reason


def test_do_not_skip_once_the_platform_has_refreshed():
	assert skip_reason_today({}, make_provider(reset_hour=8), now_hour=9) is None


def test_zero_reset_hour_still_waits_for_the_shared_checkin_hour():
	# agentrouter 零点就刷新额度，但为了和 anyrouter 攒成一封通知，仍然等到统一时间再签
	reason = skip_reason_today({}, make_provider(reset_hour=0), now_hour=0)

	assert reason is not None
	assert '一起签' in reason


def test_shared_checkin_hour_lets_both_platforms_through():
	# 到点之后两个平台都放行，同一次运行里一起到账
	assert skip_reason_today({}, make_provider(reset_hour=0), now_hour=checkin.CHECKIN_START_HOUR) is None
	assert skip_reason_today({}, make_provider(reset_hour=8), now_hour=checkin.CHECKIN_START_HOUR) is None


def test_disabling_the_shared_hour_restores_per_platform_timing(monkeypatch):
	# CHECKIN_START_HOUR=0 时恢复「各平台一到刷新点就签」
	monkeypatch.setattr(checkin, 'CHECKIN_START_HOUR', 0)

	assert skip_reason_today({}, make_provider(reset_hour=0), now_hour=0) is None


def test_platform_reset_hour_wins_when_it_is_later(monkeypatch):
	# 平台比统一时间更晚刷新时，理由要说平台没刷新，而不是「等着一起签」
	monkeypatch.setattr(checkin, 'CHECKIN_START_HOUR', 8)

	reason = skip_reason_today({}, make_provider(reset_hour=10), now_hour=9)

	assert reason is not None
	assert '10 点' in reason


def test_skip_after_the_daily_attempt_budget_is_spent():
	record = {'attempts': checkin.DAILY_ATTEMPT_LIMIT}

	reason = skip_reason_today(record, make_provider(), now_hour=9)

	assert reason is not None
	assert '不再重试' in reason


def test_attempts_below_the_budget_still_run():
	record = {'attempts': checkin.DAILY_ATTEMPT_LIMIT - 1}

	assert skip_reason_today(record, make_provider(), now_hour=9) is None


def test_rounding_jitter_does_not_count_as_credited():
	# 和记账阈值保持一致，0.01 的抖动不算到账，否则当天就再也不尝试了
	assert skip_reason_today({'reward': 0.01}, make_provider(), now_hour=9) is None


def test_new_day_keeps_balance_readout_but_clears_attempts(tmp_path, monkeypatch):
	state_file = tmp_path / 'checkin_state.json'
	monkeypatch.setattr(checkin, 'CHECK_IN_STATE_FILE', str(state_file))
	stale = {
		'date': '2000-01-01',
		'accounts': {
			'AnyRouter-zjwei': {
				'reward': 25.0,
				'at': '02:21:31',
				'max_total': 3645.75,
				'quota': 700.37,
				'used': 2945.38,
				'attempts': 2,
			}
		},
	}
	state_file.write_text(json.dumps(stale), encoding='utf-8')

	record = load_daily_state()['accounts']['AnyRouter-zjwei']

	# 余额留着给通知用，当日奖励和尝试次数必须清零，否则新的一天不会再尝试
	assert record == {'max_total': 3645.75, 'quota': 700.37, 'used': 2945.38}


def test_remember_balance_rounds_to_cents():
	record = {}

	remember_balance(record, 700.3712, 2945.3849)

	assert record == {'quota': 700.37, 'used': 2945.38}


def make_skipped_detail(reason='今日额度已到账'):
	"""构造一份「本次没发请求」的明细，余额取当日记录值"""
	return {
		'name': 'AgentRouter-L站大号',
		'before_quota': 906.04,
		'before_used': 38.96,
		'after_quota': 906.04,
		'after_used': 38.96,
		'check_in_reward': 0.0,
		'usage_increase': 0,
		'balance_change': 0,
		'success': True,
		'skipped': reason,
	}


def test_notification_tells_skipped_account_already_got_paid():
	message = format_check_in_notification(make_skipped_detail(), {'reward': 25.0, 'at': '08:57:24'})

	assert '今日额度已到账 +$25.00（08:57:24 观测到），当日不再重复登录' in message
	# 余额是当天早先记下的，标出来免得当成实时值
	assert '当日记录值' in message
	assert '今日尚未观测到额度到账' not in message


def test_notification_explains_a_skip_that_had_no_credit_yet():
	message = format_check_in_notification(make_skipped_detail(reason='额度每天 8 点后才刷新'), {})

	assert '本次跳过: 额度每天 8 点后才刷新' in message
	assert '今日额度已到账' not in message


def test_notification_still_lists_a_skipped_account_without_a_balance_readout():
	# 老状态文件里没有当天余额，也不能让账号整条从通知里消失
	detail = {
		'name': 'AgentRouter-L站小号',
		'check_in_reward': 0.0,
		'usage_increase': 0,
		'balance_change': 0,
		'success': True,
		'skipped': '今日额度已到账',
	}

	message = format_check_in_notification(detail, {'reward': 25.0, 'at': '08:57:24'})

	assert 'AgentRouter-L站小号' in message
	assert '今日额度已到账 +$25.00（08:57:24 观测到），当日不再重复登录' in message
	assert '余额:' not in message
