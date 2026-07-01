import vcstudio.shared.secrets as secrets


class FakeKeyring:
    """内存假 keyring 后端。"""
    def __init__(self):
        self.store = {}
    def set_password(self, service, name, pw):
        self.store[(service, name)] = pw
    def get_password(self, service, name):
        return self.store.get((service, name))
    def delete_password(self, service, name):
        self.store.pop((service, name), None)


def test_roundtrip_with_backend(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, 'keyring', fake)
    assert secrets.available() is True
    assert secrets.set_password('北京超算', 's3cret') is True
    assert secrets.get_password('北京超算') == 's3cret'
    secrets.delete_password('北京超算')
    assert secrets.get_password('北京超算') is None


def test_degrades_when_keyring_missing(monkeypatch):
    monkeypatch.setattr(secrets, 'keyring', None)
    assert secrets.available() is False
    assert secrets.set_password('x', 'y') is False
    assert secrets.get_password('x') is None
    secrets.delete_password('x')  # 不抛


def test_get_swallows_backend_errors(monkeypatch):
    class Boom:
        def get_password(self, *a): raise RuntimeError('backend down')
    monkeypatch.setattr(secrets, 'keyring', Boom())
    assert secrets.get_password('x') is None
