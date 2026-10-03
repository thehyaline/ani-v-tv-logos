# ani-v-tv-logos

给 [Ani-V](https://github.com/thehyaline/Ani-V) 的「电视」栏用的频道图标库：
一份「频道名 → 台标图片」的清单，客户端按频道名自动匹配图标。

直播源的频道名千奇百怪（`CCTV-1 综合`、`CCTV1综合[1080p]`、`央视一套`、`無綫翡翠台`
说的是同一个台），所以这里不按文件名匹配，而是给每个台标列一组**别名**，
客户端把名字归一化之后再查。

## 内容

- `logos/` —— 1307 个台标 PNG（透明底，宽度上限 400px，已压缩，合计约 7.8 MB）
- `index.json` —— 频道名索引，1647 个名字键
- `SOURCES.md` —— 每个文件是从哪个仓库的哪张图来的
- `build/` —— 构建脚本、来源清单、覆盖率报告

拿一份实测数字说话：对 6 份公开播放列表（粉丝明明 tv/index、ipv6、itv，
iptv-org 的 cn/hk/tw）合计 790 个频道名，命中率从 220/790（27%）提到
702/790（88%）。分列表的对照见 `build/coverage.md`。

## index.json

```json
{
  "name": "ani-v-tv-logos",
  "version": 2,
  "updated": "2026-10-04",
  "logoBase": "https://raw.githubusercontent.com/thehyaline/ani-v-tv-logos/main/logos/",
  "entries": [
    {
      "file": "CCTV1.png",
      "names": ["CCTV1", "综合频道", "央视一套", "央视1台", "CCTV-1综合", "CCTV1综合"]
    },
    {
      "file": "翡翠台.png",
      "names": ["翡翠台", "TVB翡翠台", "翡翠", "翡翠台高清"]
    }
  ]
}
```

- `file`：`logos/` 下的文件名，图片地址是 `logoBase + file`。
- `names`：这个台标认识的所有写法。繁简、大小写、空格、标点、画质后缀这些
  规则能自己吃下的**不写**在这里 —— 只有规则认不出的（副名、俗称、
  英文名、`一套`这种口语名）才列进来。
- `version`：匹配规则或结构变了才动它。

## 客户端怎么匹配

三步，命中即停：

1. **归一化后精确命中**。归一化 = 繁转简 → 全角转半角 → 丢掉空白与装饰性标点
   （`-—_·.&`、各种括号、`|`、`「」` 等）→ 折叠 Unicode 数学字母数字 → 转小写。
   所以 `ＣＣＴＶ－１　综合`、`無綫翡翠台`、`CCTV1` 各自落到 `cctv1综合`、
   `无线翡翠台`、`cctv1`。
2. **逐级剥后缀再命中**，一直剥到剥不动：画质（`高清`/`HD`/`4K`/`1080p`…）、
   编码（`H265`/`HEVC`…）、线路（`备用2`/`线路3`…）、来源（`移动`/`IPV6`…）、
   状态标签（`[Not 24/7]`）。`北京卫视超高清` 会先剥 `超高清` 而不是 `高清`。
3. **包含匹配**：表里的键落在频道名里，取最长的那个（`CCTV13新闻` 因此命中
   `cctv13` 而不是 `cctv1`）。

**不做反向包含**：频道名落在某个键里不算数 —— `KBS1` 是 `nhkbs1` 的子串，
放它过去就等于把 `NHK BS1` 的台标贴到韩国台上。短的频道名往往只是**碰巧**
是某个长名的子串，这不是证据。少匹配一张图顶多画通用图标，匹配错了是画错台。

## 怎么建出来的

```bash
python build/build_logos.py --plan       # 只算：这次要收哪些台、各自的图从哪来
python build/build_logos.py --build      # 真下：下载 → 压缩 → 写 logos/ index.json SOURCES.md
python build/build_logos.py --check      # 覆盖率：对公开播放列表算改前 vs 改后
python build/build_logos.py --selftest   # 归一化规则自检（与客户端逐字对齐）
```

几条规矩：

- **图片来源**：按 `build/build_logos.py` 里 `SOURCES` 的顺序挑，前面有的不用后面的。
- **收哪些台**：由公开播放列表决定（`REFERENCE_LISTS`）—— 列表里出现过的名字，
  能找到图就收；再加一层「背板」：图源里名字像正经频道、且长度够的也收进来。
  本地手选的那批在 `build/curated.json` 里冻着，是**输入**，重跑不会被冲掉。
- **幂等**：清单与素材都缓存在 `build/.cache/`，同一份输入跑多少遍结果都一样。
- **压缩**：宽度上限 400px，LANCZOS 缩放，调色板 256 色与直存 RGBA 各存一版取小的。

`build/coverage.md` 是 `--check` 的输出；改图源或改规则之后重新生成一份，
数字变了就能看出来。

## 加一个台标

1. 能自动挑就自动挑：把台名加进 `build/build_logos.py` 的 `ALIASES`
   （键是**索引里的文件名去掉扩展名**），或者加一条 `EXTRA_ENTRIES`
   （源里有图、脚本自己挑不出来的情况）。
2. 跑 `--plan` 看它有没有进来，再跑 `--build`。
3. 纯手工的也行：把 PNG 丢进 `logos/`，在 `index.json` 的 `entries` 里加一条。
   记得跑一次 `--check`：新加的名字**归一化后不能和已有的键撞**，
   撞了客户端先到先得，等于随机。

## 来源与许可

图**没有一张是自己画的**，全部取自下面这些公开仓库，只做了挑图、改名、压缩、
建索引这四件事。逐文件的出处见 [SOURCES.md](SOURCES.md)。

| 来源 | 用了几张 | 说明 |
|---|---|---|
| [x1ao4/tv-logos](https://github.com/x1ao4/tv-logos) | 141 | 作者声明仅供个人使用、请勿商用 |
| [vircloud/TVLogo](https://github.com/vircloud/TVLogo) | 732 | 统一 300×180 透明底，按 112114 的频道名命名 |
| [fanmingming/live](https://github.com/fanmingming/live) | 291 | GPL-3.0，社区投稿、审核后收录 |
| [taksssss/tv](https://github.com/taksssss/tv) | 58 | 量大，命名混杂 |
| [sparkssssssssss/epg](https://github.com/sparkssssssssss/epg) | 85 | 112114 台标镜像，只用来补别人都没有的台 |

台标版权归各电视台所有，此处仅作识别之用。

本库沿用上游同一份约定：**仅供个人学习与自用，请勿用于商业用途**。
权利人如果认为这里不该有某张图，提个 Issue，我们立刻删。
