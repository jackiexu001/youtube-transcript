import os

application = defines["app"]
appname = os.path.basename(application)

files = [application]
symlinks = {"Applications": "/Applications"}
icon_locations = {
    appname: (230, 220),
    "Applications": (690, 220),
}

background = defines["background"]
window_rect = ((120, 120), (920, 440))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
show_icon_preview = False
include_icon_view_settings = True
include_list_view_settings = False

icon_size = 112
text_size = 14
label_pos = "bottom"
format = "UDZO"
filesystem = "HFS+"
