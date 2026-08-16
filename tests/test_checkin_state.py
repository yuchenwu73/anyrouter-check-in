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
	save_daily_state,
)


def make_detail(reward=0.0, usage=0.0):
	"""构造一份签到明细，默认是「本次运行余额没动」"""
	return {
		'name': 'zjwei@aust.edu.cn',
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
	record_daily_reward(state, 'zjwei@aust.edu.cn', 25.0)
	save_daily_state(state)

	reloaded = load_daily_state()
	assert reloaded['accounts']['zjwei@aust.edu.cn']['reward'] == 25.0


def test_daily_state_resets_on_a_new_day(tmp_path, monkeypatch):
	state_file = tmp_path / 'checkin_state.json'
	monkeypatch.setattr(checkin, 'CHECK_IN_STATE_FILE', str(state_file))
	stale = {
		'date': '2000-01-01',
		'accounts': {'zjwei@aust.edu.cn': {'reward': 25.0, 'at': '02:21:31', 'max_total': 3645.75}},
	}
	state_file.write_text(json.dumps(stale), encoding='utf-8')

	accounts = load_daily_state()['accounts']

	# 当日奖励清零，但余额基线要留着，否则认不出间隙里到账的额度
	assert accounts['zjwei@aust.edu.cn'] == {'max_total': 3645.75}


def test_first_observation_only_seeds_the_baseline():
	state = {'date': '2026-08-16', 'accounts': {}}

	assert observe_balance(state, 'zjwei@aust.edu.cn', [3645.75, 3645.75]) == 0.0
	assert state['accounts']['zjwei@aust.edu.cn']['max_total'] == 3645.75


def test_observe_balance_detects_credit_landing_between_runs():
	# 02:20 跑完基线 3645.75；08:59 再跑时「签到前」就已经多了 $25，本次运行内零变化
	state = {'date': '2026-08-16', 'accounts': {'zjwei@aust.edu.cn': {'max_total': 3645.75}}}

	assert observe_balance(state, 'zjwei@aust.edu.cn', [3670.75, 3670.75]) == 25.0


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
	state = {'date': '2026-08-16', 'accounts': {'zjwei@aust.edu.cn': {'max_total': 3670.75}}}

	assert observe_balance(state, 'zjwei@aust.edu.cn', [100.0, 100.0]) == 0.0
	assert state['accounts']['zjwei@aust.edu.cn']['max_total'] == 100.0


def test_observe_balance_does_not_credit_twice_for_the_same_rise():
	state = {'date': '2026-08-16', 'accounts': {'zjwei@aust.edu.cn': {'max_total': 3645.75}}}

	assert observe_balance(state, 'zjwei@aust.edu.cn', [3670.75, 3670.75]) == 25.0
	assert observe_balance(state, 'zjwei@aust.edu.cn', [3670.75, 3670.75]) == 0.0


def test_record_daily_reward_accumulates_and_keeps_first_landing_time():
	state = {'date': '2026-08-16', 'accounts': {}}

	first = record_daily_reward(state, 'zjwei@aust.edu.cn', 25.0)
	second = record_daily_reward(state, 'zjwei@aust.edu.cn', 25.0)

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
