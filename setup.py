from setuptools import setup, find_packages

setup(
    name="adaos",
    version="0.1.0",
    description="AdaOS - платформа для навыков и голосового управления",
    author="AdaOS Team",
    url="https://github.com/stipot/adaos",
    packages=find_packages(),
    include_package_data=True,
    python_requires=">=3.9",
    install_requires=[
        "typer>=0.16.0",
        "rich>=13.7.1",
        "openai>=1.43.0",
        "litellm>=0.1.14",
        "GitPython>=3.1.43",
        "SQLAlchemy>=2.0.32",
        "sqlite-utils>=3.36",
        "PyYAML>=6.0.2",
        "pytest>=8.3.2",
        "watchdog>=4.0.1",
        "vosk>=0.3.45",
        "sounddevice>=0.4.7",
    ],
    entry_points={
        "console_scripts": [
            "adaos=adaos.cli:app",  # команда `adaos`
        ],
    },
)
