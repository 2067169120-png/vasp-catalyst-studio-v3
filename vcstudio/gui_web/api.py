"""pywebview js_api:集群 + 任务处理器门面。

薄处理器:每个 JS 调用由 pywebview 派独立线程执行,可同步阻塞。
铁律:每个公开方法都返回 JSON-safe dict,异常一律 try/except 兜成
{'error': str(e)},绝不让异常穿透到 JS 侧。重模块(batch_ops/ssh_test/
submitter)延迟导入,测试注入假件即可全离线跑,不碰网络/keyring。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict, fields as dc_fields


class Api:
    """js_api 门面:参数/返回全 JSON-safe;真模块延迟导入,测试注入假件。"""

    def __init__(self, *, profiles_mod=None, secrets_mod=None, ssh_test_mod=None,
                 batch_ops_mod=None, ledger_mod=None, manifest_mod=None,
                 submitter_mod=None):
        from vcstudio.cluster import profiles as _p
        from vcstudio.shared import secrets as _s
        from vcstudio.cluster import ledger as _l
        from vcstudio.shared import manifest as _m
        self._profiles = profiles_mod or _p
        self._secrets = secrets_mod or _s
        self._ledger = ledger_mod or _l
        self._manifest = manifest_mod or _m
        self._ssh_test = ssh_test_mod      # 重依赖延迟到用时 import
        self._batch_ops = batch_ops_mod
        self._submitter = submitter_mod

    # ── 桥活性探测(前端用来确认 js_api 已就绪) ──
    def ping(self) -> str:
        return 'pong'

    # ── 内部:重模块延迟加载 ──
    def _bo(self):
        if self._batch_ops is None:
            from vcstudio.cluster import batch_ops
            self._batch_ops = batch_ops
        return self._batch_ops

    def _sub(self):
        if self._submitter is None:
            from vcstudio.cluster import submitter
            self._submitter = submitter
        return self._submitter

    def _ssh(self):
        if self._ssh_test is None:
            from vcstudio.cluster import ssh_test
            self._ssh_test = ssh_test
        return self._ssh_test

    def _resolve(self, name, password):
        """名字 → (profile, 密码, err_dict|None)。

        密码优先级:显式传入 > keyring;都没有且 auth=password → 让前端弹框
        (返回精确 token 'NEED_PASSWORD',前端据此匹配)。
        """
        prof = self._profiles.load_profiles().get(name)
        if prof is None:
            return None, None, {'error': f'集群「{name}」不存在,请先在集群页保存'}
        pw = password or (self._secrets.get_password(name) if prof.auth == 'password' else None)
        if prof.auth == 'password' and not pw:
            return None, None, {'error': 'NEED_PASSWORD'}
        return prof, pw, None

    # ── 集群 ─────────────────────────────────────────────────────────────────
    def list_profiles(self):
        try:
            return {'profiles': [asdict(p) for p in self._profiles.load_profiles().values()],
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'profiles': [], 'error': str(e)}

    def save_profile(self, data: dict):
        try:
            name = str((data or {}).get('name', '')).strip()
            if not name:
                return {'ok': False, 'error': '集群名称不能为空'}
            known = {f.name for f in dc_fields(self._profiles.ClusterProfile)}
            fields = {k: v for k, v in data.items() if k in known and k != 'name'}
            allp = self._profiles.load_profiles()
            allp[name] = self._profiles.ClusterProfile(name=name, **fields)
            self._profiles.save_profiles(allp)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def delete_profile(self, name: str):
        try:
            allp = self._profiles.load_profiles()
            if name not in allp:
                return {'ok': False}
            del allp[name]
            self._profiles.save_profiles(allp)
            return {'ok': True}
        except Exception:                                 # noqa: BLE001
            return {'ok': False}

    def test_connection(self, name, password, trust_new=False):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'ok': False, 'message': f'集群「{name}」不存在,请先在集群页保存',
                        'scheduler': '', 'needs_trust': False}
            res = self._ssh().check_connection(prof, password, trust_new=bool(trust_new))
            # 成功 + 显式给了密码 + 该 profile 走密码认证 → 顺手存 keyring
            if res.ok and password and prof.auth == 'password':
                self._secrets.set_password(name, password)
            return {'ok': bool(res.ok), 'message': res.message,
                    'scheduler': res.scheduler, 'needs_trust': bool(res.needs_trust)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'message': str(e), 'scheduler': '', 'needs_trust': False}

    def has_saved_password(self, name):
        try:
            return {'saved': self._secrets.get_password(name) is not None}
        except Exception:                                 # noqa: BLE001
            return {'saved': False}

    def preview_script(self, name, job_dir):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'ok': False, 'error': f'集群「{name}」不存在,请先在集群页保存'}
            return {'ok': True, 'text': self._sub().build_script_text(prof, job_dir)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ── 任务:台账列表(取数逻辑照抄 jobs_tab.reload) ──────────────────────
    def list_jobs(self):
        try:
            jobs, stale = [], []
            for job_dir, m in self._ledger.load_all():
                if m is None:
                    stale.append(job_dir)
                    continue
                state = m.get('state', '?')
                hist = m.get('state_history') or []
                updated = hist[-1].get('at', '') if hist else m.get('created_at', '')
                res = m.get('results') or {}
                energy = res.get('energy_e0_eV', '')
                dgn = res.get('diagnosis') or {}
                live = res.get('live') or {}
                diag = ''
                if dgn.get('failure_class') and state in ('FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'):
                    diag = dgn['failure_class'] + ('♻可续算' if dgn.get('restartable') else '')
                elif state == 'RUNNING':
                    if live.get('warning'):
                        diag = '⚠' + str(live['warning'])[:24]
                    elif live.get('ionic_steps') is not None:
                        diag = f"{live['ionic_steps']}步" + (
                            f" |F|max={live['fmax']}" if live.get('fmax') else '')
                jobs.append({
                    'dir': job_dir,
                    'name': os.path.basename(os.path.normpath(job_dir)),
                    'state': state,
                    'task': f"{m.get('task_type', '')}/{m.get('calc_type', '')}",
                    'cluster': m.get('cluster') or '',
                    'job_id': m.get('scheduler_job_id') or '',
                    'energy': f'{energy:.4f}' if isinstance(energy, float) else energy,
                    'diag': diag,
                    'updated': updated,
                    'steps': live.get('ionic_steps'),
                    'fmax': live.get('fmax'),
                })
            return {'jobs': jobs, 'stale': stale}
        except Exception as e:                            # noqa: BLE001
            return {'jobs': [], 'stale': [], 'error': str(e)}

    # ── 任务:远程动作(一律先 _resolve 再委托 batch_ops 同名函数) ─────────
    def submit_jobs(self, dirs, name, password, trust_new=False):
        prof, pw, err = self._resolve(name, password)
        if err:
            return err
        try:
            return self._bo().submit_batch(prof, pw, list(dirs), bool(trust_new))
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    def fetch_jobs(self, dirs, name, password, trust_new=False, files=None):
        prof, pw, err = self._resolve(name, password)
        if err:
            return err
        try:
            if files is None:
                return self._bo().fetch_batch(prof, pw, list(dirs), bool(trust_new))
            return self._bo().fetch_batch(prof, pw, list(dirs), bool(trust_new), files)
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    def continue_jobs(self, dirs, name, password, trust_new=False):
        prof, pw, err = self._resolve(name, password)
        if err:
            return err
        try:
            return self._bo().continue_batch(prof, pw, list(dirs), bool(trust_new))
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    def refresh_status(self, name, password, trust_new=False):
        prof, pw, err = self._resolve(name, password)
        if err:
            return err
        try:
            # 目标 dirs 逻辑照抄 jobs_tab._on_refresh_status:
            # 台账里该集群 + 有作业号 + 状态 SUBMITTED/QUEUED/RUNNING
            targets = [d for d, m in self._ledger.load_all()
                       if m and m.get('scheduler_job_id') and m.get('cluster') == prof.name
                       and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')]
            if not targets:
                return {'needs_trust': False, 'results': []}
            return self._bo().refresh_batch(prof, pw, targets, bool(trust_new))
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    def queue_detail(self, name, password, trust_new=False):
        prof, pw, err = self._resolve(name, password)
        if err:
            return err
        try:
            return self._bo().queue_detail(prof, pw, bool(trust_new))
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    # ── 任务:认领外部作业 ──
    def adopt_job(self, local_dir, name, job_id, remote_dir, job_name=''):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'error': f'集群「{name}」不存在,请先在集群页保存'}
            self._sub().adopt_external_job(local_dir, prof, job_id, remote_dir, name=job_name)
            return {'ok': True}
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    # ── 台账维护 ──
    def remove_jobs(self, dirs):
        try:
            for d in dirs:
                self._ledger.unregister(d)
            return {'ok': True}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def clean_stale(self):
        try:
            stale = [d for d, m in self._ledger.load_all() if m is None]
            for d in stale:
                self._ledger.unregister(d)
            return {'ok': True, 'removed': len(stale)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ── 打开本地目录(仅 win32:os.startfile) ──
    def open_dir(self, path):
        try:
            if not os.path.isdir(path):
                return {'ok': False, 'error': '目录不存在'}
            if sys.platform == 'win32':
                os.startfile(path)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(['xdg-open', path])
            return {'ok': True}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}
