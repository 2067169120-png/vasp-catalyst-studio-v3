import pytest

from vcstudio.project.energy_gate import (
    clean_completion_from_text,
    validate_done_energy_evidence,
)


def _done_manifest(energy=-10.0):
    return {
        'state': 'DONE',
        'results': {
            'energy_e0_eV': energy,
            'diagnosis': {'failure_class': 'CONVERGED', 'exit_code': 0,
                          'clean_exit': True},
        },
    }


def test_empty_closed_vasprun_is_not_clean_completion_evidence():
    assert clean_completion_from_text(
        vasprun_text='<modeling></modeling>') == (False, '')


@pytest.mark.parametrize('vasprun_text', [
    '<modeling></modeling>',
    ('<modeling><calculation><energy><i name="e_0_energy">-99</i>'
     '</energy></calculation></modeling>'),
])
def test_outcar_ionic_event_requires_its_own_current_completion_footer(
        vasprun_text):
    outcar = ' energy(sigma->0) = -10.00000000\n'
    oszicar = ' 1 F= -10.0 E0= -10.0 d E=0\n'

    with pytest.raises(ValueError, match='OUTCAR.*timing'):
        validate_done_energy_evidence(
            _done_manifest(), 'operand', oszicar_text=oszicar,
            outcar_text=outcar, vasprun_text=vasprun_text,
            require_oszicar=True, require_current_completion=True,
            require_outcar_ionic_event=True,
        )


@pytest.mark.parametrize(('raw_energy', 'numeric_energy'), [
    (True, 1.0),
    (False, 0.0),
])
def test_manifest_boolean_is_never_accepted_as_numeric_energy(
        raw_energy, numeric_energy):
    outcar = (
        f' energy(sigma->0) = {numeric_energy:.8f}\n'
        ' General timing and accounting informations for this job:\n'
    )
    oszicar = (
        f' 1 F= {numeric_energy:.8f} E0= {numeric_energy:.8f} d E=0\n')

    with pytest.raises(ValueError, match='energy_e0_eV'):
        validate_done_energy_evidence(
            _done_manifest(raw_energy), 'operand', oszicar_text=oszicar,
            outcar_text=outcar, require_oszicar=True,
            require_current_completion=True, require_outcar_ionic_event=True,
        )
