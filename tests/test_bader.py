"""Bader 电荷解析测试(project.bader):ACF.dat / POTCAR ZVAL / ΔQ,离线合成数据。"""
import pytest

from vcstudio.project import bader

# Henkelman bader 程序的典型 ACF.dat(表头 + 分隔线 + 2 原子 + 尾部汇总)
_ACF = """\
    #         X           Y           Z       CHARGE      MIN DIST   ATOMIC VOL
 --------------------------------------------------------------------------------
    1    0.000000    0.000000    0.000000    8.147852     0.371289     7.867616
    2    1.425000    1.425000    1.425000    1.852148     0.371289     8.126547
 --------------------------------------------------------------------------------
    VACUUM CHARGE:               0.0000
    VACUUM VOLUME:               0.0000
    NUMBER OF ELECTRONS:        10.0000
"""

# 两物种 POTCAR 头部片段(真实文件的 ZVAL 行格式)
_POTCAR = """\
 PAW_PBE Fe 06Sep2000
   8.00000000000000
 parameters from PSCTR are:
   VRHFIN =Fe:  d7 s1
   POMASS =   55.847; ZVAL   =    8.000    mass and valenz
 END of PSCTR-controll parameters
 End of Dataset
 PAW_PBE O 08Apr2002
   6.00000000000000
 parameters from PSCTR are:
   VRHFIN =O: s2p4
   POMASS =   16.000; ZVAL   =    6.000    mass and valenz
 End of Dataset
"""


def test_parse_acf_text_and_file(tmp_path):
    assert bader.parse_acf(_ACF) == pytest.approx([8.147852, 1.852148])
    p = tmp_path / 'ACF.dat'
    p.write_text(_ACF, encoding='utf-8')
    assert bader.parse_acf(p) == pytest.approx([8.147852, 1.852148])  # PathLike
    assert bader.parse_acf(str(p)) == pytest.approx([8.147852, 1.852148])  # str 路径


def test_parse_acf_missing_file_chinese_guide(tmp_path):
    with pytest.raises(FileNotFoundError, match='不代跑'):
        bader.parse_acf(str(tmp_path / 'nope' / 'ACF.dat'))


def test_parse_acf_rejects_gaps_and_garbage():
    broken = _ACF.replace('    2    1.425000', '    5    1.425000')   # 序号跳变
    with pytest.raises(ValueError, match='不连续'):
        bader.parse_acf(broken)
    with pytest.raises(ValueError, match='未解析到任何原子行'):
        bader.parse_acf('VACUUM CHARGE: 0.0\nNUMBER OF ELECTRONS: 0.0\n')
    with pytest.raises(ValueError, match='CHARGE 列不是数值'):
        bader.parse_acf('  1  0.0  0.0  0.0  oops  0.3  7.8\n')
    with pytest.raises(ValueError, match='列'):
        bader.parse_acf('  1  0.0  0.0\n')                            # 列数不足


def test_read_potcar_zvals_text_and_file(tmp_path):
    assert bader.read_potcar_zvals(_POTCAR) == pytest.approx([8.0, 6.0])
    p = tmp_path / 'POTCAR'
    p.write_text(_POTCAR, encoding='utf-8')
    assert bader.read_potcar_zvals(p) == pytest.approx([8.0, 6.0])
    with pytest.raises(FileNotFoundError, match='POTCAR'):
        bader.read_potcar_zvals(str(tmp_path / 'missing_POTCAR'))
    with pytest.raises(ValueError, match='ZVAL'):
        bader.read_potcar_zvals('not a potcar\nno zval here\n')


def test_expand_zvals_by_poscar_counts():
    assert bader.expand_zvals([8.0, 6.0], [1, 2]) == pytest.approx([8.0, 6.0, 6.0])
    with pytest.raises(ValueError, match='物种数不匹配'):
        bader.expand_zvals([8.0, 6.0], [1, 2, 3])


def test_charge_transfer_delta_q():
    """ΔQ = ZVAL − CHARGE:Fe(ZVAL 8)分区 8.147852 e → ΔQ = −0.147852(得电子)。"""
    charges = bader.parse_acf(_ACF)                    # [8.147852, 1.852148]
    dq = bader.charge_transfer(charges, [8.0, 2.0])
    assert dq == pytest.approx([-0.147852, +0.147852])
    assert sum(dq) == pytest.approx(0.0)               # 电荷守恒(总电子数不变)
    with pytest.raises(ValueError, match='原子数不匹配'):
        bader.charge_transfer(charges, [8.0])


def test_full_pipeline_acf_potcar(tmp_path):
    """全流程:ACF + POTCAR(Fe×1, O×2 展开)→ 逐原子 ΔQ。"""
    acf = ('    #   X  Y  Z  CHARGE  MIN DIST  ATOMIC VOL\n'
           ' ----\n'
           '  1  0.0  0.0  0.0   6.500000  0.3  7.0\n'
           '  2  1.0  1.0  1.0   6.800000  0.3  7.0\n'
           '  3  2.0  2.0  2.0   6.700000  0.3  7.0\n'
           ' ----\n'
           '  NUMBER OF ELECTRONS:  20.0000\n')
    zvals = bader.expand_zvals(bader.read_potcar_zvals(_POTCAR), [1, 2])
    dq = bader.charge_transfer(bader.parse_acf(acf), zvals)
    # Fe: 8−6.5 = +1.5(失电子);O: 6−6.8 = −0.8、6−6.7 = −0.7(得电子)
    assert dq == pytest.approx([1.5, -0.8, -0.7])
