"""Web GUI 入口:python -m vcstudio.gui_web。

启动兜底(打包审查 P0):EXE 以 --windowed 打包,早期失败(缺 WebView2 运行时/
pythonnet、asset 路径异常)在无控制台下是纯白屏/闪退,用户连报错都拿不到。
故 main 全程 try/except:异常写 %APPDATA%/vcstudio/startup-error.log(带环境指纹),
Windows 下再弹 MessageBox 给一句可操作的中文提示。
"""
from __future__ import annotations

import os
import sys
import time
import traceback


def _startup_log_path() -> str:
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    d = os.path.join(base, 'vcstudio')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'startup-error.log')


def _report_startup_failure(exc: BaseException) -> str:
    """异常 → 日志文件(追加,带环境指纹);返回日志路径。绝不再抛。"""
    path = _startup_log_path()
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S')} 启动失败 ===\n")
            f.write(f'python={sys.version.split()[0]} frozen={getattr(sys, "frozen", False)} '
                    f'meipass={getattr(sys, "_MEIPASS", "")}\n')
            f.write(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass
    return path


def _message_box(text: str) -> None:
    """Windows 下弹阻塞消息框;其他平台/失败时退回 stderr。"""
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, 'VASP Catalyst Studio 启动失败', 0x10)
            return
    except Exception:                                     # noqa: BLE001
        pass
    print(text, file=sys.stderr)


def main() -> int:
    api = None
    try:
        import webview

        from vcstudio.gui_web import resources
        from vcstudio.gui_web.composition import create_api

        api = create_api()
        webview.create_window(
            'VASP Catalyst Studio', resources.index_html(), js_api=api,
            width=1180, height=800, min_size=(960, 640))
        service = api.start_background_services()
        if not service.get('ok'):
            raise RuntimeError('自动托管后台服务启动失败：' + str(service.get('error') or '未知错误'))
        webview.start()  # 默认 EdgeChromium(WebView2)
        return 0
    except Exception as e:                                # noqa: BLE001
        log = _report_startup_failure(e)
        hint = ''
        s = f'{type(e).__name__}: {e}'
        if 'clr' in s or 'pythonnet' in s.lower() or 'WebView2' in s or 'EdgeChromium' in s:
            hint = ('\n\n可能原因:缺少 WebView2 运行时(请安装微软 Edge WebView2 '
                    'Evergreen 运行时后重试)。')
        _message_box(f'程序未能启动:{s}{hint}\n\n详细日志:{log}')
        return 1
    finally:
        if api is not None:
            try:
                api.stop_background_services()
            except Exception:                             # noqa: BLE001
                pass


if __name__ == '__main__':
    raise SystemExit(main())
