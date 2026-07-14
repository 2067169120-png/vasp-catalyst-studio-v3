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
                 submitter_mod=None, config_mod=None, job_builder_mod=None,
                 logic_mod=None, adsorption_mod=None, report_full_mod=None,
                 dialog_fn=None):
        from vcstudio.cluster import profiles as _p
        from vcstudio.shared import secrets as _s
        from vcstudio.cluster import ledger as _l
        from vcstudio.shared import manifest as _m
        from vcstudio.shared import config as _cfg
        from vcstudio.generate import job_builder as _jb
        from vcstudio.gui import logic as _logic
        from vcstudio.project import adsorption as _ads
        self._profiles = profiles_mod or _p
        self._secrets = secrets_mod or _s
        self._ledger = ledger_mod or _l
        self._manifest = manifest_mod or _m
        # 生成页依赖(纯模块,无网络/keyring,故与 ledger/manifest 一样即时导入)
        self._config = config_mod or _cfg
        self._job_builder = job_builder_mod or _jb
        self._logic = logic_mod or _logic
        # 项目页:adsorption 纯模块(无 matplotlib)即时导入;report_full 牵扯 charts
        # (matplotlib 相邻)故延迟到用时 import(同 batch_ops),测试注入假件即免真依赖。
        self._adsorption = adsorption_mod or _ads
        self._dialog_fn = dialog_fn        # 测试注入假 dialog;None → 真走 webview
        self._ssh_test = ssh_test_mod      # 重依赖延迟到用时 import
        self._batch_ops = batch_ops_mod
        self._submitter = submitter_mod
        self._report_full = report_full_mod

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

    def _rf(self):
        """report_full 延迟加载(牵扯 charts/matplotlib 相邻,重):测试注入假件即免真依赖。"""
        if self._report_full is None:
            from vcstudio.project import report_full
            self._report_full = report_full
        return self._report_full

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
            # 前端在 keyring 已存密码时故意传 null;回退取 keyring 密码,
            # 否则 password=None 恒认证失败(存过密码后 retest 永远挂)。
            if prof.auth == 'password' and not password:
                password = self._secrets.get_password(name)
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
    def _delegate(self, name, password, fn):
        """五个远程方法的公共壳:解析集群+密码 → 委托 batch_ops;任何异常兜成 error dict。

        _resolve 内部会读 load_profiles()/keyring,配置损坏或后端报错也须在此兜住,
        绝不穿透到 JS。
        """
        try:
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            return fn(prof, pw)
        except Exception as e:                            # noqa: BLE001 异常绝不穿透到 JS
            return {'error': str(e)}

    def submit_jobs(self, dirs, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().submit_batch(
                                  prof, pw, list(dirs), bool(trust_new)))

    def fetch_jobs(self, dirs, name, password, trust_new=False, files=None):
        def _fetch(prof, pw):
            if files is None:
                return self._bo().fetch_batch(prof, pw, list(dirs), bool(trust_new))
            return self._bo().fetch_batch(prof, pw, list(dirs), bool(trust_new), files)
        return self._delegate(name, password, _fetch)

    def continue_jobs(self, dirs, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().continue_batch(
                                  prof, pw, list(dirs), bool(trust_new)))

    def refresh_status(self, name, password, trust_new=False):
        def _refresh(prof, pw):
            # 目标 dirs 逻辑照抄 jobs_tab._on_refresh_status:
            # 台账里该集群 + 有作业号 + 状态 SUBMITTED/QUEUED/RUNNING
            targets = [d for d, m in self._ledger.load_all()
                       if m and m.get('scheduler_job_id') and m.get('cluster') == prof.name
                       and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')]
            if not targets:
                return {'needs_trust': False, 'results': []}
            return self._bo().refresh_batch(prof, pw, targets, bool(trust_new))
        return self._delegate(name, password, _refresh)

    def queue_detail(self, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().queue_detail(
                                  prof, pw, bool(trust_new)))

    def query_workdir(self, job_id, name, password, trust_new=False):
        """认领辅助:按作业号查远程工作目录(PBS qstat -f;查不到 workdir='')。"""
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().workdir_lookup(
                                  prof, pw, str(job_id), bool(trust_new)))

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

    # ── 生成页(镜像 gui/generate_tab 的调用面:预览/回填/一键生成/文件选择) ─────
    def gen_preview(self, poscar_path, incar_path):
        """即时解析预览(镜像 generate_tab._refresh_preview):纯读、不写任何文件。

        summary = {'poscar','incar'} 两段中文摘要(logic.poscar_preview/incar_preview
        自身已把解析问题降级为友好文案);calc_type/validate 取生成默认(slab/开)。
        """
        try:
            poscar = (poscar_path or '').strip()
            incar = (incar_path or '').strip()
            cfg = self._config.load_config()
            lib = cfg.get('potcar_lib_root', '') or ''
            pos_txt = self._logic.poscar_preview(poscar, 'slab')
            inc_txt = self._logic.incar_preview(incar, poscar, lib, True)
            return {'ok': True, 'summary': {'poscar': pos_txt, 'incar': inc_txt},
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'summary': None, 'error': str(e)}

    def gen_state(self):
        """启动回填(镜像 generate_tab._load_state):最近 POSCAR/INCAR/输出目录 + 赝势库。"""
        try:
            cfg = self._config.load_config()
            ui = self._config.get_ui_state(cfg)
            return {'poscar': ui.get('last_poscar', '') or '',
                    'incar': ui.get('last_incar', '') or '',
                    'out_dir': ui.get('last_out', '') or '',
                    'lib_root': cfg.get('potcar_lib_root', '') or ''}
        except Exception as e:                            # noqa: BLE001
            return {'poscar': '', 'incar': '', 'out_dir': '', 'lib_root': '',
                    'error': str(e)}

    def gen_run(self, poscar_path, incar_path, out_dir, lib_root):
        """一键生成(镜像 generate_tab._on_run→build_job_dir→_write_manifest→ledger.register)。

        web 无 KPOINTS/计算类型/校验开关字段 → 取生成默认(自动 K 网格 / slab / 开校验)。
        job.yaml 与台账写入失败只追加警告,绝不撤销已生成的四件套(同 _write_manifest 口径)。
        """
        try:
            poscar = (poscar_path or '').strip()
            incar = (incar_path or '').strip()
            out = (out_dir or '').strip()
            lib = (lib_root or '').strip()
            # 持久化赝势库(_persist_lib:失败只告警,不挡生成)
            if lib:
                try:
                    self._config.set_potcar_lib_root(lib)
                except Exception:                         # noqa: BLE001
                    pass
            errs = self._logic.validate_generate_inputs(poscar, incar, out, lib)
            if errs:
                return {'ok': False, 'job_dir': None, 'warnings': [],
                        'error': ';'.join(errs)}
            validate, calc_type, kpts = True, 'slab', None
            # 路径记忆:下次启动自动回填(失败静默,同 _on_run)
            try:
                self._config.set_ui_state(last_poscar=poscar, last_incar=incar,
                                          last_out=out)
            except Exception:                             # noqa: BLE001
                pass
            payload = self._job_builder.build_job_dir(
                poscar, incar, out, calc_type=calc_type, kpoints=kpts,
                validate=validate, lib_root=lib)
            warnings = list(payload.get('warnings') or [])
            # 落 job.yaml + 登记台账(_write_manifest:失败只告警)
            try:
                self._manifest.create_from_build(
                    payload['out_dir'], payload,
                    poscar_path=poscar, validate=validate)
                self._ledger.register(payload['out_dir'])
            except Exception as e:                        # noqa: BLE001
                warnings.append(f'job.yaml/台账写入失败(不影响四件套):{e}')
            return {'ok': True, 'job_dir': payload['out_dir'],
                    'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'warnings': [], 'error': str(e)}

    # ── 吸附能项目页(镜像 gui/project_tab 调用面:列表/创建/ΔE/CSV/报告) ─────────
    def proj_list(self):
        """项目注册表 → [{path,name,n_members}](镜像 project_tab._reload_projects)。

        n_members 内联算(清洁表面 + 气相参考 + 组态族,镜像 project_tab._member_dirs):
        列表渲染绝不触碰 report_full(避免仅为计数拖入 matplotlib,matplotlib 坏时也不
        整体失败)。畸形/已移动的 project.yaml(load_project→None)或坏成员静默跳过。
        """
        try:
            projects = []
            for pp in self._adsorption.list_projects():
                try:
                    proj = self._adsorption.load_project(pp)
                    if proj is None:
                        continue
                    mem = proj.get('members') or {}
                    member_dirs = [d for d in ([mem.get('clean_slab'),
                                                mem.get('gas_ref')]
                                               + list(mem.get('configs') or [])) if d]
                    projects.append({
                        'path': pp,
                        'name': proj.get('name', '') or '',
                        'n_members': len(member_dirs),
                    })
                except Exception:                         # noqa: BLE001 单个坏项目不拖垮全表
                    continue
            return {'projects': projects, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'projects': [], 'error': str(e)}

    def proj_create(self, name, slab_path, config_paths, incar_path, gas_path,
                    out_root):
        """批量生成 清洁表面 + 组态族 +(可选)气相参考(镜像 project_tab._on_generate)。

        前置校验照抄 _on_generate;lib_root 从 config 读(失败静默)。坏组态隔离语义在
        adsorption 层已就位(create_project 的 errors 只记不拖垮全组)→ 此处把 build 警告
        与组态 errors 一并透传进 warnings,不整体失败;advisories 转成 "[级别] 文案" 列表
        (project_tab 展示口径)。gas_path 空 → ref_poscar=None(镜像可选气相参考)。
        """
        try:
            name = (name or '').strip()
            slab = (slab_path or '').strip()
            incar = (incar_path or '').strip()
            gas = (gas_path or '').strip()
            root = (out_root or '').strip()
            configs = [str(p).strip() for p in (config_paths or []) if str(p).strip()]
            errs = []
            if not name:
                errs.append('未填项目名')
            if not root:
                errs.append('未选输出根目录')
            if not slab or not os.path.isfile(slab):
                errs.append('清洁表面 POSCAR 不存在')
            if not incar or not os.path.isfile(incar):
                errs.append('共享 INCAR 不存在')
            if not configs:
                errs.append('至少添加一个吸附组态')
            if gas and not os.path.isfile(gas):
                errs.append('气相参考文件不存在')
            if errs:
                return {'ok': False, 'project_path': None, 'advisories': [],
                        'warnings': [], 'error': ';'.join(errs)}
            lib = ''
            try:
                lib = self._config.load_config().get('potcar_lib_root', '') or ''
            except Exception:                             # noqa: BLE001
                pass
            res = self._adsorption.create_project(
                os.path.join(root, name), name,
                clean_poscar=slab, config_poscars=configs,
                incar_path=incar, ref_poscar=(gas or None),
                lib_root=(lib or None))
            advisories = [f'[{pri}·{aname}] {msg}'
                          for pri, aname, msg in (res.get('advisories') or [])]
            warnings = []
            for member, _d, wlist in (res.get('generated') or []):
                for w in (wlist or []):
                    warnings.append(f'{member}:{w}')
            for member, msg in (res.get('errors') or []):
                warnings.append(f'{member}:{msg}')
            return {'ok': bool(res.get('ok')),
                    'project_path': res.get('project_path'),
                    'advisories': advisories, 'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'project_path': None, 'advisories': [],
                    'warnings': [], 'error': str(e)}

    def proj_delta(self, path):
        """项目 ΔE 汇总(镜像 project_tab._on_delta)。

        rows 忠实透传 delta_e_rows 的字段(name/state/e_config/delta_e/note);ΔE 门控语义
        原样——任一成员未 DONE 时对应行 delta_e=None 且 note 明说缺谁。note 顶层汇总清洁
        表面/气相参考状态(镜像 _on_delta 头部两行)。
        """
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'rows': [], 'note': '',
                        'error': '项目不存在或 project.yaml 已被移动'}
            s = self._adsorption.delta_e_rows(proj)
            slab_state, e_slab = s['slab']
            ref_state, e_ref = s['ref']
            rows = [{'name': r['name'], 'state': r['state'],
                     'e_config': r['e_config'], 'delta_e': r['delta_e'],
                     'note': r['note']} for r in (s.get('rows') or [])]
            parts = [f'清洁表面:{slab_state}']
            parts.append(f'气相参考:{ref_state}' if s.get('has_ref')
                         else '未设气相参考')
            return {'ok': True, 'rows': rows, 'note': ';'.join(parts),
                    'slab': {'state': slab_state, 'energy': e_slab},
                    'ref': {'state': ref_state, 'energy': e_ref,
                            'has_ref': bool(s.get('has_ref'))},
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'rows': [], 'note': '', 'error': str(e)}

    def proj_export_csv(self, path, save_to):
        """ΔE 表导出 CSV(镜像 project_tab._on_export;utf-8-sig 语义在 adsorption 层)。"""
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'file': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            out = (save_to or '').strip()
            if not out:
                return {'ok': False, 'file': None, 'error': '未指定导出路径'}
            s = self._adsorption.delta_e_rows(proj)
            result = self._adsorption.export_csv(proj, s, out)
            return {'ok': True, 'file': str(result), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'file': None, 'error': str(e)}

    def proj_report(self, path, save_to):
        """完整项目报告(镜像 project_tab._on_report);同步执行,耗时长在 JS 侧提示等待。

        load_project → 无成员作业防呆(report_full._member_dirs)→ generate_project_report
        (proj, save_to, config=load_config())。config 读失败降级为 {}(同 _on_report)。
        """
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'file': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            out = (save_to or '').strip()
            if not out:
                return {'ok': False, 'file': None, 'error': '未指定报告路径'}
            if not self._rf()._member_dirs(proj):
                return {'ok': False, 'file': None, 'error': '项目无成员作业'}
            try:
                cfg = self._config.load_config()
            except Exception:                             # noqa: BLE001
                cfg = {}
            result = self._rf().generate_project_report(proj, out, config=cfg)
            return {'ok': True, 'file': str(result), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'file': None, 'error': str(e)}

    # ── 文件/目录选择(web 无原生 input;pywebview 延迟 import,测试注入 dialog_fn) ──
    def pick_file(self, kind='poscar'):
        try:
            if self._dialog_fn is not None:
                path = self._dialog_fn(kind)
            else:
                import webview                            # 延迟:测试永不 import
                res = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG)
                path = res[0] if res else None
            return {'path': path or None}
        except Exception as e:                            # noqa: BLE001
            return {'path': None, 'error': str(e)}

    def pick_dir(self):
        try:
            if self._dialog_fn is not None:
                path = self._dialog_fn('dir')
            else:
                import webview                            # 延迟:测试永不 import
                res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
                path = res[0] if res else None
            return {'path': path or None}
        except Exception as e:                            # noqa: BLE001
            return {'path': None, 'error': str(e)}

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
