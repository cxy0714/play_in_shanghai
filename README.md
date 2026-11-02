# 上海本月去哪玩

自动汇总上海本月的展览、话剧、音乐剧和演出，生成一个可以直接分享给朋友的静态页面。

展览会自动细分为：历史文物、现代艺术、艺术家特展、数字沉浸、摄影影像、设计工艺、亲子动漫、建筑城市、自然科技、其他展览。

另外还有：

- **上海公共建筑地图**（`docs/map.html`）：把博物馆、纪念馆/故居、美术馆、剧院、会展中心、教堂、历史建筑、园林景点、文化场馆放在一张可筛选、可搜索的地图上，点列表或地图标记可以互相定位。
- **本周上新**（`docs/new.html`）：每次自动更新新增了哪些活动的时间线，可以像活动清单一样按城市、类型筛选；主页顶部也有「本周上新」摘要和卡片上的 NEW 角标。
- **全国范围**：历史文物类展览、音乐剧、话剧会额外抓取北京、广州、深圳、杭州、成都、南京、西安、武汉、苏州、天津、重庆、长沙等主要城市，详见下文「全国范围」。

## 它怎么工作

1. GitHub Actions 每周一、周四早上 8 点自动运行。
2. `update.py` 从 `config/sources.json` 里的公开网页抓取信息：上海来源 + 全国来源（`national_sources.py`）。
3. 优先读取网页里的 JSON-LD 结构化数据。
4. 没有结构化数据时，用 LLM 从网页正文里抽取活动信息（可选，当前配置不会触发）。
5. 自动去重、分类，并与上一轮的 `data/events.json` 按标题比对，算出「本次新增了哪些活动」，写进 `data/updates.json`。
6. 生成：
   - `docs/index.html`：分享给朋友的页面（顶部带 上海 / 全国 范围筛选）
   - `docs/new.html`：本周上新时间线（按城市、类型筛选）
   - `docs/events.json`：结构化数据
   - `docs/events.ics`：可导入日历的订阅文件（只含上海活动）
   - `docs/map.html`：上海公共建筑地图（由 `config/places.json` 生成）
   - `docs/places.json`：建筑地图的结构化数据

## 「本周上新」怎么算

- 口径是**标题比对**：每轮抓取完成后，把当轮活动与上一轮 `data/events.json` 的标题（去掉标点/空格后）做差集，新出现的标题记为「新增」。
- 刻意**不看日期和场馆**：猫眼/豆瓣的滚动场次每天都在变，用 id 或「标题+场馆+日期」判断会把老活动反复误报成新增。
- 每轮记录保存在 `data/updates.json`（保留最近 60 轮），`docs/new.html` 展示近 7 天的新增，更早的折叠在「更早的更新」里。
- 外地活动只有历史文物类展览、音乐剧、话剧会进入上新记录（与页面口径一致）。
- 第一次跑（没有上一轮数据）会把全部在架活动标成新增，页面会注明。
- 历史上新是用 git 历史里的 `docs/events.json` 回填的，所以页面一上线就能看到过去几周的记录。

## 全国范围

- 全国数据由 `national_sources.py` 提供，适配器名：`maoyan_city`（猫眼按城市）、`douban_city`（豆瓣同城城市站）、`museum_cn`（国博/故宫/南博/湖南博物院/陕历博/良渚博物院/中国丝绸博物馆/苏州博物馆官网展览）。
- 页面默认「上海」，切到「全国」后可以按城市筛选；外地活动只收录**历史文物类展览、音乐剧、话剧**三类。
  想放宽/收窄改 `update.py` 顶部的 `NATIONAL_GROUPS` / `NATIONAL_EXHIBITION_SUBS`。
- 加城市：在 `config/sources.json` 里加一条 `maoyan_city`，`city_id` 必须是**猫眼演出城市 id**（不是大众点评城市 id，传错会静默回落到上海）：北京 1、广州 20、深圳 30、天津 40、西安 42、重庆 45、杭州 50、南京 55、武汉 57、成都 59、长沙 70、苏州 80、上海 10。
- 全国源总量太大时可以 `"enabled": false` 关掉部分城市，或把 `categories` 从 `[9, 4]` 改成只留 `[4]`（话剧/音乐剧）或 `[9]`（展览）。
- 已知缺口：浙江省博物馆（纯 Vue 壳，接口 404）、杭州博物馆（SSL/502）抓不到；其它省级馆多为 SSL/超时/JS 渲染/需鉴权。免费常设展覆盖不足，靠猫眼售票展览兜底。

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
- `adapter`：走内置采集器（`maoyan` / `douban` / `shmuseum` / ... / `maoyan_city` / `douban_city` / `museum_cn`）
- `city` / `city_id`：只在 `maoyan_city` / `douban_city` 上用，指定城市
- `categories` / `page_size` / `max_pages`：抓取量控制
- `site`：只在 `museum_cn` 上用，选择博物馆站点（`chnmuseum` / `dpm` / `njmuseum` / `hnmuseum` / `sxhm` / `lzmuseum` / `silkmuseum` / `szmuseum`）；常设陈列不写入（源站没有真实展期）

## 建筑地图怎么维护

- `config/places.json` 是建筑清单。`places` 里每一项：
  - `name`：显示名称
  - `category`：`museum` / `gallery` / `theater` / `church` / `landmark` / `garden` / `culture`
  - `district`：所在区
  - `lat` / `lng`：WGS-84 坐标（OpenStreetMap 用的坐标系）
  - `address` / `intro`：地址和一句话介绍
  - `query`：可选，给地理编码用的搜索词（名称里有括号、别名时有用）
- 改完运行 `python update.py --places-only`，只重新生成 `docs/map.html` 和 `docs/places.json`，不会抓取活动、不调用网络。
- 页面默认使用高德底图（GCJ-02），也可以切换 OpenStreetMap（WGS-84）。构建时会同时算好两套坐标，切换底图不会偏移。
- 地图前端在 `docs/map.js` / `docs/map.css`；Leaflet 和 markercluster 已放进 `docs/vendor/`，不依赖境外 CDN。
- 底图瓦片需要联网，断网时左侧列表和筛选仍然可用。

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
