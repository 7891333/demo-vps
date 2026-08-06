# -*- coding: utf-8 -*-
"""统一入口：按 INSTANCE_ROLE 启动 manager 或 worker"""
import config

if config.ROLE == "manager":
    import manager
    manager.run()
else:
    import worker
    worker.run()