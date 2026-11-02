"""A local, dependency-free ttk theme for the office toolkit."""
from __future__ import annotations

import tkinter as tk
from tkinter import font, ttk

PALETTES = {
    'Paper': dict(background='#F4F2EC', surface='#FFFFFF', text='#202923',
                  muted='#566158', border='#818B81', accent='#246046',
                  hover='#194C36', selection='#DCEBDF', selected_text='#163C2A',
                  rail='#202C26', rail_text='#F5F4ED', rail_muted='#C2CCC3',
                  rail_hover='#33483A', disabled='#E0E4DD', disabled_text='#667066',
                  error='#A32732', desk='#E6E9E1'),
    'Ink': dict(background='#202923', surface='#29362E', text='#EEF2EB',
                muted='#BBC9BE', border='#718174', accent='#9AD4AD',
                hover='#B0E3C0', selection='#3A5744', selected_text='#F2F6EF',
                rail='#17211B', rail_text='#EEF2EB', rail_muted='#BBC9BE',
                rail_hover='#334B3B', disabled='#36433A', disabled_text='#A4B3A7',
                error='#FFACB5', desk='#19241D'),
}


def apply(root: tk.Tk, name: str) -> dict[str, str]:
    colors = PALETTES.get(name, PALETTES['Paper'])
    style = ttk.Style(root)
    style.theme_use('clam')
    available = set(font.families(root))
    family = next((face for face in ('Segoe UI', 'DejaVu Sans', 'Liberation Sans')
                   if face in available), font.nametofont('TkDefaultFont').actual('family'))
    root.ui_font_family = family
    for named in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
        font.nametofont(named).configure(family=family, size=10)
    root.option_add('*Font', (family, 10))
    root.option_add('*TCombobox*Listbox.background', colors['surface'])
    root.option_add('*TCombobox*Listbox.foreground', colors['text'])
    root.option_add('*TCombobox*Listbox.selectBackground', colors['selection'])
    root.option_add('*TCombobox*Listbox.selectForeground', colors['selected_text'])
    root.configure(background=colors['background'])
    style.configure('.', background=colors['background'], foreground=colors['text'],
                    font=(family, 10), bordercolor=colors['border'],
                    lightcolor=colors['background'], darkcolor=colors['background'],
                    focuscolor=colors['accent'], troughcolor=colors['desk'])
    style.configure('TFrame', background=colors['background'])
    style.configure('TLabel', background=colors['background'], foreground=colors['text'])
    style.configure('Muted.TLabel', foreground=colors['muted'])
    style.configure('Title.TLabel', font=(family, 22, 'bold'))
    style.configure('Brand.TLabel', font=(family, 15, 'bold'))
    style.configure('Section.TLabel', font=(family, 12, 'bold'))
    style.configure('TLabelframe', borderwidth=1, relief='solid', padding=14)
    style.configure('TLabelframe.Label', font=(family, 11, 'bold'), foreground=colors['text'])
    style.configure('TButton', padding=(12, 8), relief='flat', borderwidth=1,
                    background=colors['surface'], foreground=colors['text'])
    style.map('TButton', background=[('disabled', colors['disabled']),
              ('pressed', colors['selection']), ('active', colors['selection'])],
              foreground=[('disabled', colors['disabled_text'])],
              bordercolor=[('focus', colors['accent'])])
    primary_text = '#FFFFFF' if name != 'Ink' else '#172B1D'
    style.configure('Primary.TButton', background=colors['accent'], foreground=primary_text,
                    bordercolor=colors['accent'], font=(family, 10, 'bold'))
    style.map('Primary.TButton', background=[('disabled', colors['disabled']),
              ('pressed', colors['hover']), ('active', colors['hover'])],
              foreground=[('disabled', colors['disabled_text']), ('!disabled', primary_text)],
              bordercolor=[('focus', colors['text']), ('!focus', colors['accent'])])
    style.configure('TEntry', padding=7, fieldbackground=colors['surface'],
                    foreground=colors['text'], insertcolor=colors['text'], borderwidth=1)
    style.configure('TCombobox', padding=6, fieldbackground=colors['surface'],
                    background=colors['surface'], foreground=colors['text'], arrowcolor=colors['text'])
    style.map('TCombobox', fieldbackground=[('readonly', colors['surface'])],
              foreground=[('readonly', colors['text'])], selectbackground=[('readonly', colors['surface'])],
              selectforeground=[('readonly', colors['text'])], bordercolor=[('focus', colors['accent'])])
    style.configure('TSpinbox', padding=6, fieldbackground=colors['surface'],
                    foreground=colors['text'], arrowcolor=colors['text'])
    for control in ('TEntry', 'TSpinbox'):
        style.map(control, bordercolor=[('focus', colors['accent'])])
    for control in ('TCheckbutton', 'TRadiobutton'):
        style.configure(control, padding=(0, 4), indicatorbackground=colors['surface'],
                        indicatorforeground=colors['accent'])
        style.map(control, background=[('active', colors['background'])],
                  indicatorbackground=[('selected', colors['accent'])])
    style.configure('TNotebook', background=colors['background'], borderwidth=0, tabmargins=(0, 0, 0, 6))
    style.configure('TNotebook.Tab', padding=(16, 8), background=colors['desk'])
    style.map('TNotebook.Tab', background=[('selected', colors['surface']), ('active', colors['selection'])],
              foreground=[('selected', colors['text'])])
    style.layout('Workspace.TNotebook.Tab', [])
    style.configure('Workspace.TNotebook', borderwidth=0, tabmargins=0)
    style.configure('Treeview', background=colors['surface'], fieldbackground=colors['surface'],
                    foreground=colors['text'], rowheight=34, borderwidth=1)
    style.configure('Treeview.Heading', font=(family, 10, 'bold'), padding=(8, 9),
                    background=colors['desk'], relief='flat')
    style.map('Treeview', background=[('selected', colors['selection'])],
              foreground=[('selected', colors['selected_text'])])
    style.map('Treeview.Heading', background=[('active', colors['selection'])])
    style.configure('Horizontal.TProgressbar', background=colors['accent'],
                    borderwidth=0, thickness=4, troughcolor=colors['desk'])
    style.configure('TScrollbar', arrowsize=12, width=12, borderwidth=0,
                    background=colors['border'], troughcolor=colors['background'])
    return colors
