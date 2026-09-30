# 微信话题搜索下载器 (wechat-topic-searcher)

![version](https://img.shields.io/badge/version-0.1.0-green) ![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue) ![python](https://img.shields.io/badge/Python-3.10%2B-yellow)

输入一个话题，搜索高相关性的微信公众号文章，**AI 精排过滤跑题内容**，勾选后一键下载为本地 Markdown + 图片。单文件 exe，双击即用，正文全程不出本机。

```
输入话题 → 回车 → AI 按相关度排序打分 → 勾选 → 下载到本地
```

## 功能

- **话题搜索**：搜狗微信（腾讯自家微信垂直搜索）按文章检索，Bing / DuckDuckGo 兜底
- **相关性三层过滤**：
  - 本地规则重排（标题命中加权 + 时间新鲜度）
  - AI 语义精排：标题+摘要发给模型网关打 0-10 相关性分，跑题内容自动沉底（可开关）
  - 筛选器：时间范围 / 每号限流 / 包含词 / 排除词，改动即时生效
- **批量下载**：复用成熟爬虫核心，Markdown + 图片本地化，增量去重（重复下载自动跳过）
- **快**：搜索结果本地缓存（重复搜索毫秒级）、勾选即时、下载后台队列逐篇实时刷新
- **键盘流**：`Ctrl+F` 聚焦 · `空格` 勾选 · `Ctrl+A` 全选 · `回车` 下载 · 双击预览原文 · `F5` 强制刷新

## 快速开始

### 方式一：下载编译好的 exe

到 [Releases](https://github.com/sf00000/wechat_ai_search/releases) 下载 `wechat-topic-searcher-vX.Y.Z.exe`，双击运行。

### 方式二：从源码运行

```bash
git clone https://github.com/sf00000/wechat_ai_search.git
cd wechat_ai_search
pip install -r requirements.txt
python app.py                    # 启动
python app.py "AI 编程"          # 带话题词启动即搜
```

要求：Windows 10/11，Python 3.10+。

### 配置

首次运行前复制 `config.example.json` 为 `config.json`，按需修改：

| 字段 | 说明 |
|------|------|
| `base_dir` | 下载根目录，每个话题一个子文件夹 |
| `search_pages` | 每次搜索页数（每页 10 条） |
| `download_delay` | 文章下载间隔秒数（对微信保持礼貌） |
| `cache_ttl_minutes` | 搜索缓存有效期 |
| `sogou_cookies` | 遇搜狗验证码时填入浏览器 cookie（见下文反爬须知） |
| `ai_rerank` | 是否启用 AI 精排 |
| `api_base` / `api_token` | 模型网关地址与密钥（任意 Anthropic 协议网关；留空则读环境变量 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`） |
| `rerank_model` | 精排模型名，留空用默认 |

> 未配置网关时 AI 精排自动置灰，其余功能不受影响。

## 编译 exe

```bash
pip install -r requirements.txt pyinstaller
build_exe.bat          # 或: pyinstaller --onefile --windowed --icon assets/app.ico app.py
```

产物：`dist/wechat-topic-searcher.exe`（约 52MB）。

> `build_exe.bat` 里带了一批 `--exclude-module`：PyInstaller 会顺着
> site-packages 里某些包的可选依赖把 torch 等重型库拖进来（596MB），
> 而本项目运行时完全用不到，排除后体积回归正常。
> 爬虫核心 `vendor/wechat_scraper_v2.py` 是运行时动态加载的，
> 其依赖（markdownify/tqdm/bs4/lxml）通过 `--hidden-import` 显式声明。

## 发布新版本

1. 改 `version.py` 里的 `__version__`
2. 提交并打 tag：`git tag vX.Y.Z && git push --tags`
3. 重新运行 `build_exe.bat`，将 exe 以 `wechat-topic-searcher-vX.Y.Z.exe`
   名字上传到该 tag 的 GitHub Release

## 反爬须知

- **搜狗验证码**：搜索过于频繁会触发（弹窗提示 + 「打开搜狗验证页」按钮）。恢复方式：
  1. 浏览器打开 https://weixin.sogou.com 过一次验证，等十几分钟冷却；
  2. 推荐（立即恢复）：浏览器正常打开搜狗搜一次，F12 控制台执行
     `document.cookie`，把整串结果复制进 `config.json` 的 `sogou_cookies`。
     真实浏览器环境通常自动通过反爬检查，cookie 有效期较长。
- **微信下载限频**：爬虫内置文章间 1 秒间隔与风控熔断。个别文章瞬时失败会在
  状态列标红，重新勾选下载即可，已下载的自动跳过。

## 项目结构

```
app.py                 # PySide6 主窗口（搜索/筛选/勾选/下载/进度）
search_channels.py     # 搜狗主通道 + Bing/DDG 兜底 + 临时链接解析
rerank.py              # 本地规则重排 + 筛选器 + AI 语义精排
downloader.py          # 下载执行层（进程内调用爬虫，按话题落盘）
mdflatten.py           # Markdown 落盘辅助（平铺/更新覆盖/增量日志）
cache_store.py         # SQLite 搜索缓存 + 下载历史
version.py             # 版本号
vendor/                # 爬虫核心（来自 wechat-link-downloads skill）
assets/                # 图标（app.ico，scripts/gen_icon.py 生成）
scripts/gen_icon.py    # 图标生成脚本
tests/                 # 离线单测 + 端到端测试
build_exe.bat          # 打包脚本
```

## 测试

```bash
python tests/test_canonical.py   # 链接规范化解析单测
python tests/test_rerank.py      # 重排/筛选/AI 解析单测
python tests/e2e_test.py "话题"  # 端到端（真实搜索→勾选→下载→校验落盘）
```

## 隐私

搜索与下载全部在本机完成。AI 精排只会把**标题和摘要**发给你自己配置的模型
网关，文章正文不出本机。

## License

MIT
