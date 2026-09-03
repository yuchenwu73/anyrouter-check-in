import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin
from checkin import candidate_nodes_for, node_region

# 缩过的机场订阅：地区聚在一起，混着信息占位节点和高倍率节点
NODES = [
	'🇭🇰|香港-中转 01',
	'🇭🇰|香港-中转 02',
	'🇭🇰|香港-直连',
	'🇹🇼|台湾-IEPL 01',
	'🇹🇼|台湾-IEPL 02',
	'🇸🇬|新加坡-IEPL 01',
	'🇸🇬|新加坡-IEPL 02',
	'🇯🇵|日本-IEPL 01',
	'🇰🇷|韩国家宽-01',
	'🇦🇷|阿根廷-IEPL',
	'🇺🇦|乌克兰-IEPL 01',
]


def bind(*names):
	checkin._account_roster.clear()
	checkin._account_roster.extend(names)


def test_region_is_parsed_from_the_node_name():
	assert node_region('🇭🇰|香港-中转 01') == '香港'
	assert node_region('🇯🇵|日本星链家宽-IEPL 02') == '日本星链家宽'


def test_each_account_gets_its_own_exit_node():
	names = ['acct-a', 'acct-b', 'acct-c', 'acct-d']
	bind(*names)

	picked = [candidate_nodes_for(n, NODES)[0] for n in names]

	# 两个号共用一个出口 IP，正是平台用来关联账号的特征
	assert len(set(picked)) == len(names)


def test_the_same_account_keeps_the_same_node_across_runs():
	bind('acct-a', 'acct-b')

	first = candidate_nodes_for('acct-a', NODES)[0]
	again = [candidate_nodes_for('acct-a', NODES)[0] for _ in range(5)]

	# 一个号天天换 IP 反而可疑，所以必须每次都落回同一个节点
	assert again == [first] * 5


def test_reordering_the_config_does_not_move_anyone():
	names = ['acct-a', 'acct-b', 'acct-c']
	bind(*names)
	before = {n: candidate_nodes_for(n, NODES)[0] for n in names}

	bind(*reversed(names))
	after = {n: candidate_nodes_for(n, NODES)[0] for n in names}

	assert before == after


def test_high_latency_regions_are_never_picked_as_home():
	names = [f'acct-{i}' for i in range(8)]
	bind(*names)

	homes = [node_region(candidate_nodes_for(n, NODES)[0]) for n in names]

	# 阿根廷/乌克兰这类节点延迟高到浏览器登录必然超时，只能当兜底
	assert '阿根廷' not in homes
	assert '乌克兰' not in homes


def test_a_dead_node_falls_back_within_the_same_region_first():
	bind('only-one')

	candidates = candidate_nodes_for('only-one', NODES)
	home = node_region(candidates[0])

	# 先在同一个国家里换 IP，整个国家都不通才跨国
	assert node_region(candidates[1]) == home


def test_cold_regions_still_serve_as_a_last_resort():
	bind('only-one')

	candidates = candidate_nodes_for('only-one', NODES)

	# 兜底节点必须还在列表里，否则全挂时就没得可选了
	assert '🇦🇷|阿根廷-IEPL' in candidates


def test_accounts_outnumbering_regions_still_avoid_sharing_a_node():
	# 优先地区只有 5 个（港台新日韩）但共 9 个节点，9 个账号必然撞区，却不该撞节点
	names = [f'acct-{i}' for i in range(9)]
	bind(*names)

	picked = [candidate_nodes_for(n, NODES)[0] for n in names]

	assert len(set(picked)) == len(names)


def test_the_first_accounts_land_in_distinct_regions():
	names = [f'acct-{i}' for i in range(5)]
	bind(*names)

	regions = [node_region(candidate_nodes_for(n, NODES)[0]) for n in names]

	# 地区够用时优先散开，别让几个号挤在同一个国家
	assert len(set(regions)) == len(names)


def test_an_unknown_account_still_gets_a_node():
	bind('acct-a')

	# 名册里没有的账号（比如单账号测试路径）也不能崩，退回哈希分配
	assert candidate_nodes_for('stranger', NODES)[0] in NODES


def test_rotation_probes_the_provider_and_skips_a_node_that_cannot_reach_it(monkeypatch):
	bind('only-one')
	monkeypatch.setattr(checkin, 'PROXY_CONTROLLER', 'http://controller.test')
	monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://proxy.test')
	monkeypatch.setitem(checkin._proxy_attempt, 'only-one', 0)

	candidates = candidate_nodes_for('only-one', NODES)
	selected = {'node': None}
	probed_urls = []

	class Response:
		def __init__(self, *, status_code=200, payload=None):
			self.status_code = status_code
			self._payload = payload

		def json(self):
			return self._payload

	class Client:
		def __init__(self, *, proxy=None, timeout=None):
			self.proxy = proxy

		def __enter__(self):
			return self

		def __exit__(self, *_args):
			return None

		def get(self, url):
			if self.proxy:
				probed_urls.append(url)
				if selected['node'] == candidates[0]:
					raise OSError('目标站连接被关闭')
				return Response(status_code=403)
			return Response(payload={'all': ['AUTO', *NODES]})

		def put(self, _url, *, json):
			selected['node'] = json['name']
			return Response()

	monkeypatch.setattr(checkin.httpx, 'Client', Client)

	checkin.rotate_proxy_node('only-one', 'https://anyrouter.top')

	assert selected['node'] == candidates[1]
	assert probed_urls == ['https://anyrouter.top', 'https://anyrouter.top']
