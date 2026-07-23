from __future__ import annotations

import types

from vcstudio.gui_web.api import Api


class _Chat:
    def __init__(self):
        self.sent = []
        self.local = []
        self.attached = []

    def list_sessions(self):
        return [{'id': 's1', 'title': 'Chat'}]

    def create_session(self, title=None):
        return {'id': 's2', 'title': title or 'New chat'}

    def history(self, session_id, include_previews=True):
        return [{'id': 1, 'role': 'assistant', 'content': 'ready',
                 'source': 'local', 'attachments': []}]

    def list_attachments(self, session_id, include_previews=True):
        return [{'id': 'a1', 'name': 'INCAR', 'size_bytes': 12}]

    def attach(self, session_id, path):
        item = {'id': f'a{len(self.attached) + 2}', 'name': path.split('/')[-1],
                'size_bytes': 10}
        self.attached.append((session_id, path))
        return item

    def local_reply(self, session_id, user_text, assistant_text):
        self.local.append((session_id, user_text, assistant_text))
        return {'status': 'local', 'content': assistant_text}

    def send(self, session_id, text, attachment_ids=None):
        self.sent.append((session_id, text, list(attachment_ids or [])))
        return {'status': 'ok', 'content': 'model reply'}

    def stop(self, session_id):
        return {'session_id': session_id, 'stopped': True, 'request_id': 'r1'}


def _config(allow_external):
    return types.SimpleNamespace(
        load_config=lambda: {
            'llm': {
                'allow_external': allow_external,
                'base_url': 'https://example.invalid/v1',
                'model': 'test',
            },
        },
    )


def test_chat_api_keeps_local_commands_off_model_and_status_is_read_only():
    chat = _Chat()
    adsorption = types.SimpleNamespace(
        list_projects=lambda: [],
        load_project=lambda _path: None,
    )
    api = Api(assistant_chat_mod=chat, config_mod=_config(False),
              adsorption_mod=adsorption)

    help_result = api.ai_chat_send('s1', '/help')
    status_result = api.ai_chat_send('s1', '/status')

    assert help_result['ok'] is True and status_result['ok'] is True
    assert chat.sent == []
    assert '/status' in chat.local[0][2]
    assert '没有找到' in chat.local[1][2]


def test_chat_api_requires_external_consent_and_forwards_only_selected_ids():
    blocked_chat = _Chat()
    blocked = Api(assistant_chat_mod=blocked_chat, config_mod=_config(False))
    result = blocked.ai_chat_send('s1', 'analyse', ['a1'])
    assert result['ok'] is False and '默认关闭' in result['error']
    assert blocked_chat.sent == []

    chat = _Chat()
    enabled = Api(assistant_chat_mod=chat, config_mod=_config(True))
    result = enabled.ai_chat_send('s1', 'analyse', ['a1'])
    assert result['ok'] is True
    assert chat.sent == [('s1', 'analyse', ['a1'])]


def test_stop_with_extra_text_is_local_and_cannot_bypass_external_consent():
    chat = _Chat()
    api = Api(assistant_chat_mod=chat, config_mod=_config(False))

    result = api.ai_chat_send('s1', '/stop should-stay-local', ['a1'])

    assert result['ok'] is True
    assert chat.sent == []
    assert '/stop 不接受附加文本' in chat.local[-1][2]


def test_chat_api_imports_explicit_files_and_stop_never_touches_hpc():
    chat = _Chat()
    api = Api(assistant_chat_mod=chat, config_mod=_config(True),
              dialog_fn=lambda kind: ['/tmp/INCAR', '/tmp/results.zip'])

    picked = api.pick_files('chat')
    attached = api.ai_chat_attach('s1', picked['paths'])
    stopped = api.ai_chat_stop('s1')

    assert picked['paths'] == ['/tmp/INCAR', '/tmp/results.zip']
    assert attached['ok'] is True and len(attached['attachments']) == 2
    assert chat.attached == [('s1', '/tmp/INCAR'), ('s1', '/tmp/results.zip')]
    assert stopped['result']['stopped'] is True


def test_chat_api_reports_partial_attachment_success_truthfully():
    class PartialChat(_Chat):
        def attach(self, session_id, path):
            if path.endswith('bad.zip'):
                raise ValueError('ZIP path traversal')
            return super().attach(session_id, path)

    chat = PartialChat()
    api = Api(assistant_chat_mod=chat, config_mod=_config(False))

    result = api.ai_chat_attach('s1', ['/tmp/INCAR', '/tmp/bad.zip'])

    assert result['ok'] is False
    assert [item['name'] for item in result['attachments']] == ['INCAR']
    assert result['rejected'] == [
        {'name': 'bad.zip', 'error': 'ZIP path traversal'},
    ]
    assert '已成功导入的文件仍保留' in result['error']


def test_chat_report_command_generates_diagnostic_when_final_gate_is_blocked(tmp_path):
    chat = _Chat()
    project_path = str(tmp_path / 'project.yaml')
    project = {'name': 'Catalyst A', 'root': str(tmp_path)}
    adsorption = types.SimpleNamespace(
        list_projects=lambda: [project_path],
        load_project=lambda path: project if path == project_path else None,
        delta_e_rows=lambda _project: {'rows': []},
    )
    api = Api(assistant_chat_mod=chat, config_mod=_config(False),
              adsorption_mod=adsorption)
    api._final_report_gate = lambda _project, _summary: (False, '参考态尚未完成')
    calls = []
    api.proj_report = lambda path, out, final=False: (
        calls.append((path, out, final))
        or {'ok': True, 'file': out, 'files': [out, out[:-5] + '.docx'],
            'error': None}
    )

    result = api.ai_chat_send('s1', '/report Catalyst A')

    assert result['ok'] is True
    assert calls == [
        (project_path, str(tmp_path / 'report' / 'Catalyst A_diagnostic.html'), False),
    ]
    assert '诊断报告已生成' in chat.local[-1][2]
    assert '参考态尚未完成' in chat.local[-1][2]
