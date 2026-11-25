"""Shared desktop palette for the live center and its configuration wizard."""
import tkinter as tk
from tkinter import ttk
import ctypes
from ctypes import wintypes

BG = '#f1f5fc'
SURFACE = '#fbfdff'
INK = '#152443'
MUTED = '#465d80'
BLUE = '#165cf3'


def initialize(window):
    window.configure(bg=BG)
    if hasattr(ctypes, 'windll'):
        def caption():
            if not window.winfo_exists():
                return
            user = ctypes.windll.user32.GetAncestor
            user.argtypes = [wintypes.HWND, wintypes.UINT]
            user.restype = wintypes.HWND
            hwnd = user(window.winfo_id(), 2)
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            for attribute, color in ((35, 0x8C4517), (36, 0xFFFFFF), (34, 0x8C4517)):
                value = wintypes.DWORD(color)
                set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        window.after_idle(caption)
    style = ttk.Style(window)
    style.configure('Live.TFrame', background=BG)
    style.configure('Live.TLabel', background=BG, foreground=INK, font=('Microsoft YaHei UI', 10))
    style.configure('Live.TLabelframe', background=SURFACE, bordercolor='#cbdaf1', relief='solid')
    style.configure('Live.TLabelframe.Label', background=BG, foreground=INK, font=('Microsoft YaHei UI', 10, 'bold'))
    if 'LiveButton.border' not in style.element_names():
        style.element_create('LiveButton.border', 'from', 'clam', 'Button.border')
    for name, bg, fg in (('Live', SURFACE, INK), ('LivePrimary', BLUE, 'white')):
        key = name + '.TButton'
        style.layout(key, [('LiveButton.border', {'sticky': 'nswe', 'children': [
            ('Button.padding', {'sticky': 'nswe', 'children': [('Button.label', {'sticky': 'nswe'})]})
        ]})])
        style.configure(key, background=bg, foreground=fg, bordercolor='#c6d5ec', padding=(12, 7),
                        font=('Microsoft YaHei UI', 10, 'bold'))
        style.map(key, background=[('disabled', '#e6edf8'), ('active', '#154ed0' if name == 'LivePrimary' else '#e7effe')],
                  foreground=[('disabled', '#6e83a1')])
    style.configure('Live.Treeview', background=SURFACE, fieldbackground=SURFACE, foreground=INK,
                    rowheight=30, font=('Microsoft YaHei UI', 10))
    style.configure('Live.Treeview.Heading', background='#e8effa', foreground=INK,
                    font=('Microsoft YaHei UI', 10, 'bold'), padding=(5, 7))
    style.map('Live.Treeview', background=[('selected', '#dbeaff')], foreground=[('selected', INK)])


def metric(parent, label, value):
    box = ttk.LabelFrame(parent, text=label, padding=(12, 8), style='Live.TLabelframe')
    options = {'textvariable': value} if isinstance(value, tk.Variable) else {'text': value}
    tk.Label(box, **options, bg=SURFACE, fg=BLUE, font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    return box
