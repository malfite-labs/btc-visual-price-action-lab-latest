"""Tk-owned Kline child; no browser fallback or service lifecycle operations."""
import ctypes, json, re, subprocess, tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox

ROOT=Path(__file__).resolve().parents[1]
KLINE_URL='http://127.0.0.1:5175/kline'
PREFS=ROOT/'.run'/'kline-window.json'
HOST=ROOT/'.run'/'kline-host'/'KlineHost.exe'
_current=None

def sanitize_geometry(value):
    value=str(value or '')
    match=re.fullmatch(r'(\d{3,4})x(\d{3,4})([+-]\d{1,5}[+-]\d{1,5})?',value)
    if not match:return '1440x900'
    width,height=int(match[1]),int(match[2])
    return value if 1000<=width<=7680 and 650<=height<=4320 else '1440x900'

def open_kline(root):
    global _current
    if _current is not None and _current.winfo_exists():
        _current.deiconify();_current.lift();_current.focus_force();return _current
    if not HOST.is_file():
        messagebox.showerror('K线窗口不可用','WebView2 原生宿主尚未构建；请参阅安装说明。',parent=root);return None
    win=tk.Toplevel(root);win.title('BTC · K线交易观察');win.minsize(1000,650)
    # Tk transient windows lose Windows minimize/maximize. Set a Win32 owner
    # on the standard overlapped Tk window and retain its system buttons.
    win.update_idletasks()
    if hasattr(ctypes,'windll'):
        user=ctypes.windll.user32
        get_parent=user.GetAncestor
        get_parent.argtypes=[wintypes.HWND,wintypes.UINT];get_parent.restype=wintypes.HWND
        set_owner=getattr(user,'SetWindowLongPtrW',None)
        if set_owner:
            set_owner.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t];set_owner.restype=ctypes.c_ssize_t
            owner=get_parent(root.winfo_id(),2)
            child=get_parent(win.winfo_id(),2)
            if owner and child:set_owner(child,-8,owner)
    try:prefs=json.loads(PREFS.read_text(encoding='utf8'));geometry=prefs.get('geometry')
    except (OSError,ValueError,AttributeError):geometry=None
    win.geometry(sanitize_geometry(geometry));frame=tk.Frame(win,bg='#10161f');frame.pack(fill='both',expand=True);win.update_idletasks()
    try:
        process=subprocess.Popen([str(HOST),str(frame.winfo_id())],cwd=str(HOST.parent),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except OSError as exc:
        win.destroy();messagebox.showerror('K线窗口不可用',str(exc),parent=root);return None
    _current=win
    def close():
        global _current
        try:
            PREFS.parent.mkdir(parents=True,exist_ok=True);PREFS.write_text(json.dumps({'geometry':sanitize_geometry(win.geometry())}),encoding='utf8')
        except OSError:pass
        win.destroy();_current=None
    def check():
        if not win.winfo_exists():return
        if process.poll() is not None:
            close();messagebox.showerror('K线窗口不可用','原生宿主已退出，请检查 WebView2 Runtime。',parent=root);return
        win.after(1000,check)
    win.protocol('WM_DELETE_WINDOW',close);win.after(1000,check);return win
