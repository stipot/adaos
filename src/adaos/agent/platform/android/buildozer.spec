[app]
title = AdaOS
package.name = adaos
package.domain = org.adaos
source.dir = .
source.include_exts = py,kv,txt,md
requirements = python3,kivy,pyjnius
android.api = 31
android.minapi = 23
android.archs = arm64-v8a, armeabi-v7a
orientation = portrait
fullscreen = 0

# Разрешения (на будущее — микрофон/аудио)
android.permissions = RECORD_AUDIO, MODIFY_AUDIO_SETTINGS, WAKE_LOCK, FOREGROUND_SERVICE

# Если понадобится foreground service — добавим в следующий шаг
# android.add_src = service/java  (когда появится)

[buildozer]
log_level = 2
warn_on_root = 1