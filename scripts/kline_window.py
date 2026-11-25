"""Tk-owned Kline child; no browser fallback or service lifecycle operations."""
import ctypes, json, re, subprocess, tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

ROOT=Path(__file__).resolve().parents[1]
KLINE_URL='http://127.0.0.1:5175/kline'
PREFS=ROOT/'.run'/'kline-window.json'
HOST=ROOT/'.run'/'kline-host'/'KlineHost.exe'
_windows={}

def sanitize_geometry(value, screen=None):
    value=str(value or '')
    match=re.fullmatch(r'(\d{3,4})x(\d{3,4})([+-]\d{1,5}[+-]\d{1,5})?',value)
    width,height=(int(match[1]),int(match[2])) if match else (1440,900)
    if not (760<=width<=7680 and 500<=height<=4320):width,height=1440,900
    if screen is None:
        return f'{width}x{height}' + (match[3] or '' if match and (width,height)==(int(match[1]),int(match[2])) else '')
    sw,sh=screen
    width,height=min(width,sw-48),min(height,sh-115)
    x,y=max(0,(sw-width)//2),max(0,(sh-height-55)//2)
    if match and match[3]:
        coords=re.fullmatch(r'\+(\d+)\+(\d+)',match[3])
        if coords and int(coords[1])+width+16<=sw and int(coords[2])+height+72<=sh:
            x,y=int(coords[1]),int(coords[2])
    return f'{width}x{height}+{x}+{y}'

def _attach_demo_feed(window, snapshot):
    if snapshot is None or getattr(window, '_demo_feed', None):
        return

    class Feed(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            origin = 'http://127.0.0.1:5175'
            if (self.path != '/demo-fills' or self.headers.get('Origin') != origin
                    or self.headers.get('Host') != '127.0.0.1:5187'):
                self.send_error(403)
                return
            try:
                payload = json.dumps(snapshot(), ensure_ascii=False).encode('utf-8')
            except (OSError, ValueError, KeyError, RuntimeError):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.send_header('Access-Control-Allow-Origin',origin)
            self.end_headers()
            self.wfile.write(payload)

    try:
        server = ThreadingHTTPServer(('127.0.0.1',5187),Feed)
    except OSError:
        return
    window._demo_feed = server
    Thread(target=server.serve_forever,daemon=True).start()

    def stop(event):
        if event.widget is window:
            def close():
                server.shutdown()
                server.server_close()
            Thread(target=close,daemon=True).start()

    window.bind('<Destroy>',stop,add='+')


def open_kline(root, *, live=False, preview=False, demo_snapshot=None):
    if preview and not live:
        raise ValueError('Preview is only supported for the isolated live chart')
    key = (live, preview)
    current=_windows.get(key)
    if current is not None and current.winfo_exists():
        _attach_demo_feed(current,demo_snapshot)
        current.deiconify();current.lift();current.focus_force();return current
    if not HOST.is_file():
        messagebox.showerror('K线窗口不可用','WebView2 原生宿主尚未构建；请参阅安装说明。',parent=root);return None
    win=tk.Toplevel(root);win.title('BTC · 实盘K线' if live else 'BTC · K线交易观察')
    screen=(win.winfo_screenwidth(),win.winfo_screenheight())
    win.minsize(min(760,screen[0]-48),min(500,screen[1]-115))
    settings=PREFS.with_name('live-kline-preview.json' if preview else 'live-kline-window.json') if live else PREFS
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
    try:prefs=json.loads(settings.read_text(encoding='utf8'));geometry=prefs.get('geometry')
    except (OSError,ValueError,AttributeError):geometry=None
    win.geometry(sanitize_geometry(geometry,screen));frame=tk.Frame(win,bg='#10161f');frame.pack(fill='both',expand=True);win.update_idletasks()
    try:
        arguments=[str(HOST),str(frame.winfo_id())]
        if live: arguments.append('live-preview' if preview else 'live')
        process=subprocess.Popen(arguments,cwd=str(HOST.parent),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except OSError as exc:
        win.destroy();messagebox.showerror('K线窗口不可用',str(exc),parent=root);return None
    _windows[key]=win
    _attach_demo_feed(win,demo_snapshot)
    def close():
        try:
            settings.parent.mkdir(parents=True,exist_ok=True);settings.write_text(json.dumps({'geometry':win.geometry()}),encoding='utf8')
        except OSError:pass
        win.destroy();_windows.pop(key,None)
    def check():
        if not win.winfo_exists():return
        if process.poll() is not None:
            close();messagebox.showerror('K线窗口不可用','原生宿主已退出，请检查 WebView2 Runtime。',parent=root);return
        win.after(1000,check)
    win.protocol('WM_DELETE_WINDOW',close);win.after(1000,check);return win


def open_live_kline(root, *, demo_snapshot=None):
    return open_kline(root, live=True, demo_snapshot=demo_snapshot)
