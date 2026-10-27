# SPDX-License-Identifier: GPL-3.0-or-later
"""从 MiHome-Windows 移植的核心逻辑层（GPL-3.0）。

本目录只保留与界面无关的纯逻辑，剔除了原项目 app/core 中依赖 Qt 或
Windows 桌面的模块：

    保留   _json_store  cache  icon_store  models  net_guard
           schedule_store  scheduler  service  spec_zh
    剔除   jobs（Qt）        update_checker（Qt + GitHub Releases）
           settings_store（winreg 自启动）  board_store（桌面挂件）
           tray_store（系统托盘）           workbench_store（桌面布局）

其中 service.py 是全项目唯一 import mijiaAPI 的模块，这一隔离是原作者
刻意设计的，也是本次能低成本 Web 化的前提。
"""
