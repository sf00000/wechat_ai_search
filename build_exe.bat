@echo off
chcp 65001 >nul
REM Build single-file exe: dist\wechat-topic-searcher.exe, then run --selftest.
REM wechat_scraper_v2 (vendor/) is loaded dynamically at runtime, so PyInstaller
REM cannot see its deps -- listed via hidden-import.
REM torch/transformers/... are pulled in by optional-import chains in
REM site-packages; our runtime never touches them -- excluded to keep the
REM exe small (596MB -> 52MB).
cd /d %~dp0
python -m PyInstaller --noconfirm --onefile --windowed ^
  --icon assets\app.ico ^
  --add-data "assets\app.ico;assets" ^
  --name wechat-topic-searcher ^
  --paths vendor --hidden-import wechat_scraper_v2 ^
  --hidden-import markdownify --hidden-import tqdm ^
  --hidden-import bs4 --hidden-import lxml ^
  --exclude-module torch --exclude-module torchvision --exclude-module torchaudio ^
  --exclude-module accelerate --exclude-module transformers --exclude-module datasets ^
  --exclude-module safetensors --exclude-module tokenizers --exclude-module sentencepiece ^
  --exclude-module numpy --exclude-module pandas --exclude-module scipy ^
  --exclude-module sklearn --exclude-module matplotlib --exclude-module sympy ^
  --exclude-module networkx --exclude-module jinja2 --exclude-module filelock ^
  --exclude-module fsspec --exclude-module IPython --exclude-module jupyter ^
  --exclude-module cv2 --exclude-module skimage --exclude-module PIL ^
  app.py
if errorlevel 1 goto :fail

REM 构建产物守卫：site-packages 里某些包的可选依赖会拖入冲突运行库
REM （icuuc/icudt 与 Qt6Core 冲突即 "DLL load failed"，torch 系则是体积爆炸）
findstr /C:"icuuc" build\wechat-topic-searcher\PKG-00.toc >nul 2>&1
if not errorlevel 1 goto :fail
findstr /C:"icudt" build\wechat-topic-searcher\PKG-00.toc >nul 2>&1
if not errorlevel 1 goto :fail
findstr /C:"torch\lib" build\wechat-topic-searcher\PKG-00.toc >nul 2>&1
if not errorlevel 1 goto :fail

REM 打包后自检：验证爬虫核心在包体内可加载（打包缺陷在此即暴露）
dist\wechat-topic-searcher.exe --selftest
if errorlevel 1 goto :fail

echo.
echo Done: dist\wechat-topic-searcher.exe
exit /b 0

:fail
echo.
echo BUILD FAILED
exit /b 1
