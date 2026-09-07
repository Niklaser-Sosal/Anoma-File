import os
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import webview

from engine.converter_core import convert_file

APP_NAME = "Anoma-File"
APP_VERSION = "2.4.1"
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
ICON_PATH = BASE_DIR / "assets" / "anoma-file.ico"


def resource_path(rel):
    base = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    return base / rel


class Api:
    def __init__(self):
        self.window = None

    def set_window(self, window):
        self.window = window

    def _js(self, script):
        if self.window:
            try:
                self.window.evaluate_js(script)
            except Exception:
                pass

    @staticmethod
    def _model_filters():
        # pywebview validates the description part of a Windows file filter.
        # A slash in the description (e.g. "BNMA/BINOMA") is rejected.
        return ("BNMA + BINOMA (*.bnma;*.binoma)", "All files (*.*)")

    def _open_dialog(self, allow_multiple=False, file_types=()):
        try:
            return self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=allow_multiple,
                file_types=file_types,
            )
        except Exception as exc:
            # Keep the picker usable even if a particular WebView backend
            # rejects a custom filter. An unfiltered native dialog is a safe
            # fallback; the selected path is validated by the converter.
            try:
                self._js("window.anomaLog(" + _json_js(
                    f"[WARN] Фильтр окна выбора не поддержан: {exc}. Открываю все файлы."
                ) + ");")
                return self.window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=allow_multiple,
                    file_types=("All files (*.*)",),
                )
            except Exception:
                raise

    def choose_input(self):
        result = self._open_dialog(allow_multiple=False, file_types=self._model_filters())
        return result[0] if result else ""

    def choose_inputs(self):
        result = self._open_dialog(allow_multiple=True, file_types=self._model_filters())
        return list(result or [])

    def choose_output(self, suggested="output.glb"):
        result = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=Path(suggested).name if suggested else "output.glb",
            file_types=("GLB model (*.glb)", "All files (*.*)")
        )
        return result or ""

    def choose_fbx(self, suggested="skeleton.fbx"):
        result = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=Path(suggested).name if suggested else "skeleton.fbx",
            file_types=("FBX model (*.fbx)", "All files (*.*)")
        )
        return result or ""

    def convert(self, opts):
        input_path = str(opts.get("input") or "")
        output_path = str(opts.get("output") or "")
        extract_fbx = bool(opts.get("extractFbx"))
        fbx_output = str(opts.get("fbxOutput") or "")
        embed_texture = bool(opts.get("embedTexture", True))
        fast_texture = not bool(opts.get("slowPng"))

        if not input_path or not os.path.isfile(input_path):
            raise ValueError("Входной файл не найден.")
        if not output_path:
            raise ValueError("Не указан путь GLB результата.")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        def worker():
            self._js("window.anomaProgress(8); window.anomaStatus('Распаковка BNMA…');")
            try:
                def logger(message):
                    safe = str(message).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
                    self._js(f"window.anomaLog('{safe}');")
                    # Conversion is mostly CPU work; keep the bar moving without faking completion.
                    if "Распаковка" in str(message):
                        self._js("window.anomaProgress(15);")
                    elif "Встроенный FBX" in str(message):
                        self._js("window.anomaProgress(28);")
                    elif "Embedded DDS" in str(message):
                        self._js("window.anomaProgress(35);")
                    elif "Mesh" in str(message) or "mesh" in str(message):
                        self._js("window.anomaProgress(55);")
                    elif "GLB готов" in str(message):
                        self._js("window.anomaProgress(96);")

                result = convert_file(
                    input_path,
                    output_path,
                    extract_fbx=extract_fbx,
                    fbx_output=fbx_output or None,
                    embed_texture=embed_texture,
                    fast_texture=fast_texture,
                    log=logger,
                )
                self._js("window.anomaProgress(100); window.anomaDone(%s);" % _json_js(result))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._js("window.anomaError(%s);" % _json_js(message))
                traceback.print_exc()

        threading.Thread(target=worker, daemon=True).start()
        return True


def _json_js(value):
    import json
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def register_associations():
    if os.name != "nt":
        return
    try:
        import winreg
        exe = str(Path(sys.executable).resolve())
        icon = f'"{exe}",0'
        command = f'"{exe}" "%1"'
        for ext, progid in ((".bnma", "AnomaFile.BNMA"), (".binoma", "AnomaFile.BINOMA")):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\\Classes\\{ext}") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, progid)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\\Classes\\{progid}") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{APP_NAME} model")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\\Classes\\{progid}\\DefaultIcon") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\\Classes\\{progid}\\shell\\open\\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except Exception:
        pass


def initial_files():
    return [a for a in sys.argv[1:] if str(a).lower().endswith((".bnma", ".binoma")) and os.path.isfile(a)]


def main():
    register_associations()
    api = Api()
    html = resource_path("web/index.html")
    window = webview.create_window(
        APP_NAME,
        str(html),
        js_api=api,
        width=1360,
        height=860,
        min_size=(1050, 700),
        resizable=True,
        text_select=False,
    )
    api.set_window(window)

    def on_loaded():
        files = initial_files()
        if files:
            _json = _json_js(files[0])
            api._js(f"window.anomaOpenFile({_json});")

    webview.start(on_loaded, icon=str(resource_path("assets/anoma-file.ico")), debug=False)


if __name__ == "__main__":
    main()
