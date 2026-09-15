"""Li-S preparation must treat a PAW_PBE quartet without GGA as PBE."""

from vcstudio.gui_web.api import Api


def test_paw_pbe_default_conflicts_with_rpbe_reference(tmp_path):
    incar = tmp_path / 'INCAR'
    incar.write_text('ENCUT=400\nISPIN=1\nIVDW=0\nLDAU=F\n', encoding='utf-8')
    signatures = {'Li2S8': {
        'functional': 'RPBE', 'ivdw': 0, 'ispin': 1, 'ldau': 'F',
        'encut': 400.0,
        'potcar_titel': ['PAW_PBE Li', 'PAW_PBE S'],
        'potcar_elements': ['Li', 'S'],
    }}

    check = Api._reference_method_check(
        signatures, str(incar),
        planned_potcar=['PAW_PBE Li', 'PAW_PBE S'],
        planned_element_orders=[['Li', 'S']])

    assert check['planned']['functional'] == 'PBE'
    assert check['status'] == 'incompatible'
    assert any('functional' in issue and 'RPBE' in issue and 'PBE' in issue
               for issue in check['issues'])
