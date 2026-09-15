from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_platform_helpers_do_not_drift_back_to_repository_root():
    assert list(ROOT.glob('*.bat')) == []
    assert list(ROOT.glob('*.cmd')) == []
    assert not (ROOT / '_待处理归档').exists()

    assert (ROOT / 'packaging' / 'windows' / 'build-exe.cmd').is_file()
    assert (ROOT / 'tools' / 'windows' / 'clean-generated.cmd').is_file()
    assert (ROOT / 'docs' / 'archive' / 'README.md').is_file()
    assert (
        ROOT
        / 'docs'
        / 'archive'
        / 'legacy-process'
        / '2026-07-05-夜间优化完成报告.md'
    ).is_file()


def test_generated_cleanup_preserves_artifacts_and_research_results():
    cleanup = (
        ROOT / 'tools' / 'windows' / 'clean-generated.cmd'
    ).read_text(encoding='utf-8')
    build = (
        ROOT / 'packaging' / 'windows' / 'build-exe.cmd'
    ).read_text(encoding='utf-8')

    assert 'packaging\\build_exe.py --full' in build
    assert 'rmdir /s /q "dist"' not in cleanup
    assert 'rmdir /s /q "results"' not in cleanup
    assert 'Preserving dist\\ artifacts and results\\ research outputs.' in cleanup
