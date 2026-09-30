@echo off
chcp 65001 >nul
REM Build single-file exe: dist\wechat-topic-searcher.exe
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
echo.
echo Done: dist\wechat-topic-searcher.exe
