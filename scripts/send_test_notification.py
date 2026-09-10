#!/usr/bin/env python3
"""发一封测试通知，用来确认邮件排版；不签到、不碰账号、也不改状态文件

各平台额度总览取状态缓存里当天记下的余额，所以恢复了缓存就是真实数字；
没有缓存时只说明读不到，不编造数字。
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from checkin import load_daily_state, summarize_provider_balances  # noqa: E402
from utils.config import load_accounts_config  # noqa: E402
from utils.notify import notify  # noqa: E402


def main():
	accounts = load_accounts_config() or []
	daily_state = load_daily_state()

	overview = summarize_provider_balances(accounts, {}, daily_state)
	blocks = ['\n'.join(overview) if overview else '[BALANCE] 状态缓存里没有余额读数，本次统计不出各平台额度']

	blocks.append(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
	blocks.append(
		'\n'.join(
			[
				'[TEST] 这是一封手动触发的测试通知，没有执行签到，也没有改动任何账号状态。',
				f'[TEST] 额度总览覆盖 {len(accounts)} 个账号，数字取自最近一次签到运行记下的余额。',
				'[TEST] 真实签到通知里，总览下面还会跟上每个账号的签到明细和成功率统计。',
			]
		)
	)

	content = '\n\n'.join(blocks)
	print(content)
	notify.push_message('AnyRouter Check-in 测试通知', content, msg_type='text')


if __name__ == '__main__':
	main()
