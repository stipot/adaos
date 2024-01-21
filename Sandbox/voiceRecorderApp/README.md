## Демонстративный вариант приложения для записи и распознавания голоса

Приложение позволяет записывать голос, при нажатии на кнопку и мгновенно распознает речь, отображая распознанный текст на экране.
Для распознавания речи использовалась библиотека [speech-recognition](https://www.npmjs.com/package/@capacitor-community/speech-recognition "speech recognition npm"). Работа данной библиотеки не поддерживается в web, поэтому приложение работает только на android устройствах.

### Основные комманды:

### установка необходимых зависимостей

```
npm install
```

### запуск билда приложения

`ionic build`

### запуск приложения в браузере для отладки

`npm start`
или
`ionic serve`

### добавление платформы android

`ionic cap add android`

## Запуск приложения на android устройстве через capacitor

- Потребуются [android sdk](https://developer.android.com/studio "android sdk") и [java jdk](https://www.oracle.com/java/technologies/downloads/#jdk21-windows "java jdk")
- Чтобы компьютер видел android устройство, нужно включить отладку по usb на нем и подключить по usb проводу к компьютеру
- Также нужно установить глобальные переменные `ANDROID_HOME` и `JAVA_HOME` на компьюетере значением указывется пути к android sdk и java jdk
- После чего добавляем платформу android в проект `ionic cap add android` (если есть пропускаем этот пункт)
- Билдим приложение `ionic build`
- Теперь можно запускать приложение на подключенном android устройстве `npm android start` или `ionic cap run android --livereload --external`

## Запуск в android studio

- открыть папку android в Android Studio, если ее нет - написать команду `ionic cap add android`
- если возникает ошибка при сборке, нужно закрыть проект в android studio, удалить его, в vs code удалить папку android, написать команду `ionic cap add android` и еще раз открыть папку android в Android Studio
- для корректной работы нужно устнановить sdk с api 24 и выше (tools => sdk manager)
- для запуска на физическом устройстве нужно разрешить отладку по usb на нем и подключть по usb к компьютеру
