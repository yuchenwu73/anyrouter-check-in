import json

from utils.config import AccountConfig, AppConfig, ProviderConfig


def test_builtin_provider_profile_persistence_defaults(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is True
	assert config.providers['agentrouter'].persist_profile is False


def test_provider_profile_persistence_can_override_builtin(monkeypatch):
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps(
			{
				'anyrouter': {'domain': 'https://anyrouter.top', 'persist_profile': False},
				'agentrouter': {'domain': 'https://agentrouter.org', 'persist_profile': True},
			}
		),
	)

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].persist_profile is False
	assert config.providers['agentrouter'].persist_profile is True


def test_custom_provider_profile_persistence_defaults_to_false(monkeypatch):
	monkeypatch.setenv('PROVIDERS', json.dumps({'custom': {'domain': 'https://custom.example.com'}}))

	config = AppConfig.load_from_env()

	assert config.providers['custom'].persist_profile is False


def test_provider_from_dict_inherits_profile_persistence_from_defaults():
	defaults = ProviderConfig(name='custom', domain='https://old.example.com', persist_profile=True)

	provider = ProviderConfig.from_dict(
		'custom',
		{'domain': 'https://new.example.com'},
		defaults=defaults,
	)

	assert provider.persist_profile is True


def test_account_can_force_proxy_regardless_of_provider():
	# 被 WAF 按「账号 + 出口 IP」拉黑的账号需要直接走代理，不必先撞一次 403
	account = AccountConfig.from_dict({'name': 'blocked', 'email': 'e', 'password': 'p', 'use_proxy': True}, 0)

	assert account.resolve_use_proxy(False) is True


def test_account_can_force_direct_connection():
	account = AccountConfig.from_dict({'name': 'direct', 'cookies': {'session': 's'}, 'use_proxy': False}, 0)

	assert account.resolve_use_proxy(True) is False


def test_account_without_proxy_setting_follows_provider():
	account = AccountConfig.from_dict({'name': 'plain', 'cookies': {'session': 's'}}, 0)

	assert account.resolve_use_proxy(True) is True
	assert account.resolve_use_proxy(False) is False


def test_account_ignores_non_boolean_proxy_setting():
	account = AccountConfig.from_dict({'name': 'weird', 'cookies': {'session': 's'}, 'use_proxy': 'yes'}, 0)

	assert account.resolve_use_proxy(False) is False
def test_builtin_providers_carry_their_quota_reset_hour(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	# anyrouter 早 8 点才刷新额度，agentrouter 是零点
	assert config.providers['anyrouter'].checkin_reset_hour == 8
	assert config.providers['agentrouter'].checkin_reset_hour == 0


def test_custom_provider_reset_hour_defaults_to_midnight(monkeypatch):
	monkeypatch.setenv('PROVIDERS', json.dumps({'custom': {'domain': 'https://custom.example.com'}}))

	config = AppConfig.load_from_env()

	assert config.providers['custom'].checkin_reset_hour == 0


def test_provider_reset_hour_can_override_builtin(monkeypatch):
	monkeypatch.setenv('PROVIDERS', json.dumps({'anyrouter': {'domain': 'https://anyrouter.top', 'checkin_reset_hour': 6}}))

	config = AppConfig.load_from_env()

	assert config.providers['anyrouter'].checkin_reset_hour == 6


def test_provider_from_dict_inherits_reset_hour_from_defaults():
	defaults = ProviderConfig(name='anyrouter', domain='https://anyrouter.top', checkin_reset_hour=8)

	provider = ProviderConfig.from_dict('anyrouter', {'domain': 'https://anyrouter.top'}, defaults=defaults)

	assert provider.checkin_reset_hour == 8
