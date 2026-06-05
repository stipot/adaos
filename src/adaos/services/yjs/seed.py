from __future__ import annotations

SEED: dict = {
    "ui": {
        "application": {
            "version": "0.2",
            "desktop": {
                "topbar": [],
                "iconTemplate": {"icon": "apps-outline"},
                "widgetTemplate": {"style": {"minWidth": 240}},
                "pageSchema": {
                    "id": "desktop",
                    "title": "Рабочий стол",
                    "layout": {
                        "type": "single",
                        "areas": [{"id": "main", "role": "main"}],
                    },
                    "widgets": [
                        {
                            "id": "desktop-icons",
                            "type": "collection.grid",
                            "area": "main",
                            "title": "Приложения",
                            "inputs": {"columns": 6},
                            "dataSource": {
                                "kind": "y",
                                "transform": "desktop.icons",
                            },
                            "actions": [
                                {
                                    "on": "select",
                                    "type": "openModal",
                                    "params": {"modalId": "$event.action.openModal"},
                                },
                            ],
                        },
                        {
                            "id": "desktop-widgets",
                            "type": "desktop.widgets",
                            "area": "main",
                            "title": "Виджеты",
                            "dataSource": {
                                "kind": "y",
                                "transform": "desktop.widgets",
                            },
                        },
                    ],
                },
            },
            "modals": {
                "settings": {
                    "title": "Настройки",
                    "type": "scenario-settings",
                },
                "apps_catalog": {
                    "title": "Доступные приложения",
                    "schema": {
                        "id": "apps_catalog",
                        "layout": {
                            "type": "single",
                            "areas": [{"id": "main", "role": "main"}],
                        },
                        "widgets": [
                            {
                                "id": "apps-list",
                                "type": "collection.grid",
                                "area": "main",
                                "title": "Приложения",
                                "dataSource": {
                                    "kind": "y",
                                    "path": "data/catalog/apps",
                                },
                                "actions": [
                                    {
                                        "on": "select",
                                        "type": "callHost",
                                        "target": "desktop.toggleInstall",
                                        "params": {
                                            "type": "app",
                                            "id": "$event.id",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                },
                "widgets_catalog": {
                    "title": "Доступные виджеты",
                    "schema": {
                        "id": "widgets_catalog",
                        "layout": {
                            "type": "single",
                            "areas": [{"id": "main", "role": "main"}],
                        },
                        "widgets": [
                            {
                                "id": "widgets-list",
                                "type": "collection.grid",
                                "area": "main",
                                "title": "Виджеты",
                                "dataSource": {
                                    "kind": "y",
                                    "path": "data/catalog/widgets",
                                },
                                "actions": [
                                    {
                                        "on": "select",
                                        "type": "callHost",
                                        "target": "desktop.toggleInstall",
                                        "params": {
                                            "type": "widget",
                                            "id": "$event.id",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                },
            },
            "registry": {
                "widgets": [],
                "modals": [],
            },
        }
    },
    "data": {
        "catalog": {
            "apps": [],
            "widgets": [],
        },
        "installed": {"apps": [], "widgets": []},
    },
}
