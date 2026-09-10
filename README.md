# 上海本月去哪玩

自动汇总上海本月的展览、话剧、音乐剧和演出，生成一个可以直接分享给朋友的静态页面。

## 它怎么工作

1. GitHub Actions 每周一、周四早上 8 点自动运行。
2. `update.py` 从 `config/sources.json` 里的公开网页抓取信息。
3. 优先读取网页里的 JSON-LD 结构化数据。
4. 没有结构化数据时，用 LLM 从网页正文里抽取活动信息。
5. 自动去重、分类、生成：
   - `docs/index.html`：分享给朋友的页面
   - `docs/events.json`：结构化数据
   - `docs/events.ics`：可导入日历的订阅文件

## 一次性设置

1. 把仓库推到 GitHub。
2. 打开仓库的 **Settings → Pages**。
3. Source 选择 **Deploy from a branch**，Branch 选择 `main`，目录选择 `/docs`。
4. 打开 **Actions** 页面，允许 workflow 运行。
5. 手动点一次 **更新上海活动清单 → Run workflow**，生成第一批页面。
6. 页面地址通常是：
   `https://你的用户名.github.io/play_in_shanghai/`

## 关于 LLM

默认会尝试用 GitHub Actions 自带的 `GITHUB_TOKEN` 调用 GitHub Models，模型默认 `gpt-4o-mini`，不需要额外配置。

如果 GitHub Models 不可用，可以在仓库里配置：

- Secret：`LLM_API_KEY`
- Variables：
  - `LLM_BASE_URL`，例如 `https://api.deepseek.com/v1`
  - `LLM_MODEL`，例如 `deepseek-chat`

也可以直接用其他 OpenAI 兼容接口。没有配置 LLM 时，程序只会尝试 JSON-LD / RSS，能抓到多少算多少，不会报错中断。

## 数据源怎么改

编辑 `config/sources.json`：

- `name`：来源名称
- `url`：要抓取的页面
- `reader`：`jina` 表示用 Jina Reader 把动态网页转成文本；`direct` 表示直接抓 HTML
- `extractor`：`auto` / `jsonld` / `rss`
- `category_hint`：给 LLM 的类别提示
- `enabled`：是否启用

## 本地预览

```bash
pip install -r requirements.txt
python update.py
# 然后直接打开 docs/index.html
```

只想测试不调用 LLM：

```bash
python update.py --no-llm
```

## 说明

- 页面信息来自公开网页，最终以官方购票/预约页面为准。
- 票务平台和公众号可能调整页面结构；如果某天抓不到，页面上会保留上一次成功结果，不会清空历史。
- 这个项目适合个人和朋友小范围分享，不建议直接用于商业公开分发。
