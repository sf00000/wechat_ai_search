# vendor/

`wechat_scraper_v2.py` —— 微信公众号文章爬虫核心（本项目唯一的外来模块）。

## 来源

- 上游：`wechat-link-downloads` skill（sf00000 自有代码，未公开发布）
- 引入方式：整体拷贝，**未做任何修改**（文件头注释与实现保持上游原样）
- 引入版本：2026-09-30 快照
- 许可证：MIT（随本项目 LICENSE；代码作者即本项目作者）

## 上游更新策略

本文件为快照式 vendor，不自动跟随上游。若上游有修复，手工覆盖本文件并在
提交说明中登记。运行时也可用环境变量 `WECHAT_SKILL_DIR` 指向本机 skill
目录，临时使用上游最新版验证（见 `downloader.py::_load_scraper`）。

## 依赖

requests / beautifulsoup4 / markdownify / tqdm / lxml（见根目录 requirements.txt）。

## 本项目对其的使用方式

- `scrape_wechat(urls, delay, images_dir, account_dir, progress_callback)`：
  批量抓取入口（顺序抓取 + 逐篇回调 + 风控熔断）
- `_is_wechat_host(url)`：hostname 白名单校验
- `_sanitize_filename_part(s)`：文件名清洗

下游封装见 `downloader.py`；Markdown 平铺/增量日志等落盘辅助已从上游
download_articles.py 抽出为独立模块 `mdflatten.py`（同源，同等许可）。
