from __future__ import annotations

import os
from types import SimpleNamespace

from vcstudio.gui_web.api import Api


def test_stable_map_uses_reference_species_and_falls_back_from_incomplete_marker():
    api = Api()
    project = {'name': 'Legacy catalyst'}
    summary = {
        'rows': [
            {'name': 'unfinished', 'reference_species': 'Li2S4',
             'delta_e': None, 'is_most_stable': True},
            {'name': 'top', 'reference_species': 'Li2S4',
             'delta_e': -1.1, 'is_most_stable': False},
            {'name': 'bridge', 'reference_species': 'Li2S4',
             'delta_e': -1.3, 'is_most_stable': False},
        ],
    }

    assert api._proj_stable_delta_map(project, summary) == {'Li2S4': -1.3}


def test_cross_catalyst_method_check_allows_spin_but_blocks_functional_conflict():
    def record(label, functional, spin):
        return {
            'label': label,
            'fingerprint': {
                'functional': functional,
                'dispersion': 'none',
                'encut': 520.0,
                'spin': spin,
                'kpoints_scheme': 'mesh 3x3x1',
                'potcar_ids': {'S': 'PAW_PBE S'},
            },
            'known': {
                'functional': True,
                'dispersion': True,
                'encut': True,
                'spin': True,
                'u_by_element': True,
                'kpoints_scheme': True,
                'potcar_ids': True,
            },
            'u_by_element': {'S': {'L': -1, 'U': 0.0, 'J': 0.0}},
            'evidence_warnings': [],
        }

    spin_only = Api._comparison_method_pair(
        record('A', 'PBE', 1), record('B', 'PBE', 2), 'A', 'B')
    functional = Api._comparison_method_pair(
        record('A', 'PBE', 1), record('B', 'HSE06', 1), 'A', 'B')

    assert spin_only['status'] == 'verified'
    assert functional['status'] == 'incompatible'
    assert any('泛函' in issue and '不一致' in issue
               for issue in functional['issues'])


def test_cross_catalyst_reference_signature_blocks_different_reference_energies():
    same = Api._comparison_reference_issue(
        {'S8': -10.0, 'Li2S': -20.0},
        {'S8': -10.0005, 'Li2S': -20.0005},
    )
    different = Api._comparison_reference_issue(
        {'S8': -10.0, 'Li2S': -20.0},
        {'S8': -10.0, 'Li2S': -20.01},
    )

    assert same is None
    assert different['blocking'] is True
    assert 'Li2S' in different['message']


def test_eight_catalyst_projects_share_one_ladder_and_keep_authoritative_ul(tmp_path):
    projects = {}
    summaries = {}
    for index in range(8):
        path = str(tmp_path / f'catalyst-{index}' / 'project.yaml')
        project = {
            'name': f'Catalyst {index + 1}',
            'root': str(tmp_path / f'catalyst-{index}'),
        }
        projects[path] = project
        summaries[project['name']] = {
            'slab': ('DONE', -100.0),
            'method_consistency': {'status': 'verified'},
            'rows': [
                {'name': f'site-a-{index}', 'species': 'Li2S4',
                 'delta_e': -1.0 - index / 10},
                {'name': f'site-b-{index}', 'species': 'Li2S4',
                 'delta_e': -1.2 - index / 10},
            ],
        }

    adsorption = SimpleNamespace(
        load_project=lambda path: projects.get(path),
        delta_e_rows=lambda project: summaries[project['name']],
    )
    calls = []

    class NativeCharts:
        @staticmethod
        def free_energy_ladder(paths, out_path, **kwargs):
            calls.append((paths, str(out_path), kwargs))
            return [os.path.abspath(str(out_path))]

    api = Api(adsorption_mod=adsorption, native_charts_mod=NativeCharts)

    def fed(project, _summary):
        index = int(project['name'].split()[-1]) - 1
        return {
            'steps': [
                {'label': 'S8', 'G': 0.0},
                {'label': 'Li2S8', 'G': -0.2 - index / 20},
                {'label': 'Li2S4', 'G': -0.8 - index / 20},
            ],
            'pds_index': index % 2,
            'u_l': 1.90 - index / 100,
        }, None

    api._proj_fed = fed
    result = api.proj_compare_figures(
        list(projects), ['ladder'], str(tmp_path / 'comparison'))

    assert result['ok'] is True
    assert result['skipped'][0]['kind'] == 'ladder'
    assert result['skipped'][0]['partial'] is True
    assert len(calls) == 1
    paths, output, kwargs = calls[0]
    assert len(paths) == 8
    assert output.endswith('multi_catalyst_free_energy.png')
    assert kwargs['show_ul'] is True
    assert [path['name'] for path in paths] == [
        f'Catalyst {index + 1}' for index in range(8)
    ]
    assert [path['pds_index'] for path in paths] == [index % 2 for index in range(8)]
    assert [path['u_l'] for path in paths] == [
        1.90 - index / 100 for index in range(8)
    ]
    assert all(item['G'][0] == 0.0 for item in paths)
    for index, project in enumerate(projects.values()):
        stable = api._proj_stable_delta_map(project, summaries[project['name']])
        assert stable['Li2S4'] == -1.2 - index / 10


def test_partial_ladder_exclusion_is_returned_to_the_frontend(tmp_path):
    paths = [str(tmp_path / f'p{index}.yaml') for index in range(3)]
    projects = {
        path: {'name': name, 'root': str(tmp_path)}
        for path, name in zip(paths, ('A', 'B', 'C'))
    }
    summaries = {
        name: {
            'slab': ('DONE', -10.0),
            'rows': [{'name': 'site', 'species': 'Li2S4', 'delta_e': -1.0}],
            'method_consistency': (
                {'status': 'incompatible', 'issues': ['泛函不一致']}
                if name == 'C' else {'status': 'verified'}),
        }
        for name in ('A', 'B', 'C')
    }
    adsorption = SimpleNamespace(
        load_project=lambda path: projects.get(path),
        delta_e_rows=lambda project: summaries[project['name']],
    )

    class NativeCharts:
        @staticmethod
        def free_energy_ladder(_paths, out_path, **_kwargs):
            return [str(out_path)]

    api = Api(adsorption_mod=adsorption, native_charts_mod=NativeCharts)
    api._proj_fed = lambda _project, _summary: ({
        'steps': [{'label': 'S8', 'G': 0.0}, {'label': 'Li2S8', 'G': -0.2}],
        'pds_index': 0,
        'u_l': -0.2,
    }, None)

    result = api.proj_compare_figures(paths, ['ladder'], str(tmp_path / 'out'))

    assert result['ok'] is True
    assert len(result['files']) == 1
    assert result['skipped'][0]['kind'] == 'ladder'
    assert result['skipped'][0]['partial'] is True
    assert 'C: 方法不兼容（泛函不一致）' in result['skipped'][0]['reason']
