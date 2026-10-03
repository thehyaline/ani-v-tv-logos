# ani-v-tv-logos

给 [Ani-V](https://github.com/thehyaline/Ani-V) 的「电视」栏用的频道图标库：
一份「频道名 → 台标图片」的清单，客户端按频道名自动匹配图标。

直播源的频道名千奇百怪（`CCTV-1 综合 高清`、`CCTV1综合`、`央视一套` 说的是同一个台），
所以这里不按文件名匹配，而是给每个台标列一组**别名**，客户端归一化后再查。

## 内容

- `logos/` —— 141 个台标 PNG（透明底，宽度上限 640px，已压缩）
- `index.json` —— 频道名索引

## index.json

```json
{
  "name": "ani-v-tv-logos",
  "version": 1,
  "updated": "2026-10-04",
  "logoBase": "https://raw.githubusercontent.com/thehyaline/ani-v-tv-logos/main/logos/",
  "entries": [
    {
      "file": "CCTV1.png",
      "names": ["CCTV1", "综合频道", "央视一套", "央视1台", "CCTV-1综合", "CCTV1综合"]
    },
    {
      "file": "CCTV-风云足球.png",
      "names": ["CCTV风云足球", "风云足球", "CCTV-风云足球"]
    }
  ]
}
```

- `file`：`logos/` 下的文件名，图片地址是 `logoBase + file`。
- `names`：这个台标认识的所有写法。归一化（全角转半角、去空白、去 `-—_·.&`、转小写）
  之后**不能有两个台标撞到同一个键** —— 撞了的话谁匹配到就是随机的。
- `version`：匹配规则或结构变了才动它。

客户端匹配顺序：归一化精确命中 → 逐级剥掉画质后缀（高清 / HD / 4K / 8K / FHD …）再命中
→ 按名字长度从长到短做包含匹配（`CCTV13新闻` 因此命中 `cctv13` 而不是 `cctv1`）。

## 加台标

1. 把 PNG 放进 `logos/`（文件名别带空格，中文可以）。
2. 在 `index.json` 的 `entries` 里加一条，`names` 写全别名。
3. 提交推送即可，客户端换图标库地址就会重新拉一遍。

## 来源与许可

图标取自 [x1ao4/tv-logos](https://github.com/x1ao4/tv-logos)，作者声明**仅供个人使用，请勿商用**。
本库只做了整理：挑配色（深色界面上看得清的）、改名、压缩，并按频道名建索引。

因此本库沿用同一份约定：**仅供个人学习与自用，请勿用于商业用途**。
台标版权归各电视台所有，此处仅作识别之用。
