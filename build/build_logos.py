#!/usr/bin/env python3
"""把几个公开台标库整理成 Ani-V 用的图标库。

产物（都在仓库根）：
  logos/<频道>.png   每个频道一张，扁平、去重、按频道名命名，宽度上限 400px
  index.json         频道名（含别名）→ 图标文件的索引，App 直接读它做匹配
  build/manifest.json  每个产物的来源（哪个仓库的哪个文件）与哈希，可追溯、可增量
  SOURCES.md         分来源的署名与逐文件清单

用法：
  python build/build_logos.py --plan       只算「要哪些台、从哪来」，不下载不写盘
  python build/build_logos.py --build      下载缺失素材、压缩、写产物（幂等，可反复跑）
  python build/build_logos.py --check      拿公开播放列表算覆盖率（改前 vs 改后）
  python build/build_logos.py --selftest   钉住归一化与匹配的规则（与 App 侧同一批用例）

**归一化与匹配规则必须和 App 侧逐字一致**，两边对不上就是匹配不上：
  App 侧：`lib/data/live/tv_logo_index.dart`
  脚本侧：本文件 normalize / strip_suffix / Matcher 三节（--selftest 用同一批用例钉住）
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / 'build' / '.cache'
LOGOS = ROOT / 'logos'
INDEX = ROOT / 'index.json'
MANIFEST = ROOT / 'build' / 'manifest.json'
CURATED = ROOT / 'build' / 'curated.json'
COVERAGE = ROOT / 'build' / 'coverage.md'
SOURCES_MD = ROOT / 'SOURCES.md'

RAW = 'https://raw.githubusercontent.com/{repo}/HEAD/{path}'
TREE = 'https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1'

# OpenCC 的繁→简表（Apache-2.0）。App 侧的 t2s.dart 也是从这一份生成的
# （E:\Repo\Ani-V\tool\gen_t2s.py），取法一致：多值时取第一个，只收单字条目。
OPENCC_TS = ('https://raw.githubusercontent.com/BYVoid/OpenCC/master/'
             'data/dictionary/TSCharacters.txt')

# 台标最多这么宽。电视上一格台标也就一两百像素宽，再大是白占体积。
MAX_WIDTH = 400

# 「背板」：公开列表没点名也收进来的台，只从这几个源里收。
# 另外两个源（taksssss、sparkssssssssss）只按需补缺 —— 它们的文件名里混着
# 拼音缩写、hex 串、`1855DA` 这种内部编号，当背板会收进来一大片不是台名的东西。
BACKBONE_SOURCES = {'x1ao4', 'vircloud', 'fanmingming'}

# 产物里「不是台名」的名字（网络用语、占位符）直接丢掉。
JUNK_KEYS = {
    'test', 'test1', 'test2', 'demo', 'null', 'none', 'unknown', 'empty',
    '未知', '未知频道', '测试', '测试台', '未定', '待定', '新频道', '综合',
}

# ---------------------------------------------------------------------------
# 图源。**顺序就是优先级**：前一个源里有的台，不会用后一个源的图。
#
# 排在前面的理由不是名次，是「这份图在这台设备上好不好看」：
# x1ao4 那套是照着深色界面挑的配色，vircloud 是统一的 300×180 透明底，
# fanmingming 是社区投稿审核过的，taksssss 是大杂烩（量最大、质量参差），
# sparkssssssssss 是 112114 的镜像（脏名多，只拿来补别人都没有的台）。
# ---------------------------------------------------------------------------
SOURCES = [
    {
        'id': 'x1ao4',
        'repo': 'x1ao4/tv-logos',
        'prefix': 'tv-logos/',
        # 只取这三层：根目录那批 + 两套深色界面上看得清的 CCTV 配色。
        # 其余的配色（透明白底/透明黑底/灰白渐变）在深色界面上要么糊要么脏。
        'dirs': ['',
                 'CCTV 红白红黑/CCTV 红白',
                 'CCTV 风云频道 红白红黑/CCTV 风云频道 红白'],
        'credit': '[x1ao4/tv-logos](https://github.com/x1ao4/tv-logos)',
        'note': '作者声明仅供个人使用、请勿商用',
    },
    {
        'id': 'vircloud',
        'repo': 'vircloud/TVLogo',
        'prefix': '',
        'dirs': [''],
        'credit': '[vircloud/TVLogo](https://github.com/vircloud/TVLogo)',
        'note': '统一 300×180 透明底，按 112114 的频道名命名',
    },
    {
        'id': 'fanmingming',
        'repo': 'fanmingming/live',
        'prefix': 'tv/',
        'dirs': [''],
        'credit': '[fanmingming/live](https://github.com/fanmingming/live)（GPL-3.0）',
        'note': '社区投稿、审核后收录',
    },
    {
        'id': 'taksssss',
        'repo': 'taksssss/tv',
        'prefix': 'icon/',
        'dirs': [''],
        'credit': '[taksssss/tv](https://github.com/taksssss/tv)',
        'note': '量大，命名混杂（中文名 + 拼音缩写）',
    },
    {
        'id': 'sparks',
        'repo': 'sparkssssssssss/epg',
        'prefix': 'logo/',
        'dirs': None,  # 整棵树都要（里面按 cn/kr/uk 分了子目录）
        'credit': '[sparkssssssssss/epg](https://github.com/sparkssssssssss/epg)',
        'note': '112114 台标镜像，只用来补别人都没有的台',
    },
]

# 拿来做「目标频道清单」与覆盖率报告的公开播放列表。这些列表是真实用户会订阅的
# 那种，频道名怎么写的都有 —— 库里收哪些台，就是这个清单说了算。
REFERENCE_LISTS = [
    ('fanmingming-index', 'fanmingming/live', 'tv/m3u/index.m3u', '粉丝明明 tv/index.m3u'),
    ('fanmingming-ipv6', 'fanmingming/live', 'tv/m3u/ipv6.m3u', '粉丝明明 tv/ipv6.m3u'),
    ('fanmingming-itv', 'fanmingming/live', 'tv/m3u/itv.m3u', '粉丝明明 tv/itv.m3u'),
    ('iptv-org-cn', 'iptv-org/iptv', 'streams/cn.m3u', 'iptv-org streams/cn.m3u'),
    ('iptv-org-hk', 'iptv-org/iptv', 'streams/hk.m3u', 'iptv-org streams/hk.m3u'),
    ('iptv-org-tw', 'iptv-org/iptv', 'streams/tw.m3u', 'iptv-org streams/tw.m3u'),
]

# ---------------------------------------------------------------------------
# 别名表：规范名（索引里那个文件去掉扩展名的名字）→ 额外认识的写法。
# 归一化规则能自己吃下的（繁简、大小写、空格、标点、画质后缀、线路后缀）不写在这里，
# 这里只写**规则认不出**的：副名、俗称、品牌前缀、「一套」这种口语名。
# ---------------------------------------------------------------------------
ALIASES = {
    # 央视
    'CCTV1': ['综合频道', '央视一套', '央视1台', 'CCTV-1综合', 'CCTV1综合', 'CCTV1综合频道'],
    'CCTV2': ['财经频道', '央视二套', '央视2台', 'CCTV-2财经', 'CCTV2财经'],
    'CCTV3': ['综艺频道', '央视三套', '央视3台', 'CCTV-3综艺'],
    'CCTV4': ['中文国际频道', '央视四套', '央视4台', 'CCTV-4中文国际'],
    'CCTV5': ['体育频道', '央视五套', '央视5台', 'CCTV-5体育'],
    'CCTV5+': ['CCTV-5+', '体育赛事频道', '央视5+', 'CCTV5plus', 'CCTV5+体育赛事'],
    'CCTV6': ['电影频道', '央视六套', '央视6台', 'CCTV-6电影'],
    'CCTV7': ['国防军事频道', '央视七套', '央视7台', 'CCTV-7国防军事'],
    'CCTV8': ['电视剧频道', '央视八套', '央视8台', 'CCTV-8电视剧'],
    'CCTV9': ['纪录频道', '央视九套', '央视9台', 'CCTV-9纪录'],
    'CCTV10': ['科教频道', '央视十套', '央视10台', 'CCTV-10科教'],
    'CCTV11': ['戏曲频道', '央视十一套', '央视11台'],
    'CCTV12': ['社会与法频道', '央视十二套', '央视12台', 'CCTV-12社会与法'],
    'CCTV13': ['新闻频道', '央视新闻频道', '央视十三套', '央视13台'],
    'CCTV14': ['少儿频道', '央视十四套', '央视14台'],
    'CCTV15': ['音乐频道', '央视十五套', '央视15台'],
    'CCTV16': ['奥林匹克频道', '央视十六套', '央视16台', 'CCTV-16奥林匹克'],
    'CCTV17': ['农业农村频道', '央视十七套', '央视17台', 'CCTV-17农业农村'],
    'CCTV4K': ['CCTV-4K', '央视4K', 'CCTV4K超高清', 'CCTV4K频道'],
    'CCTV8K': ['CCTV-8K', '央视8K', 'CCTV8K超高清'],
    'CCTV4-美洲': ['CCTV-4AME', 'CCTV4AME', '中文国际美洲', 'CCTV4美洲'],
    'CCTV4-欧洲': ['CCTV-4EUO', 'CCTV4EUO', '中文国际欧洲', 'CCTV4欧洲'],
    'CCTV-世界地理': ['世界地理', 'CCTV世界地理', '央视世界地理'],
    'CCTV-兵器科技': ['兵器科技', 'CCTV兵器科技', '央视兵器科技'],
    'CCTV-卫生健康': ['卫生健康', 'CCTV卫生健康', '央视卫生健康'],
    'CCTV-发现之旅': ['发现之旅', 'CCTV发现之旅', '央视发现之旅'],
    'CCTV-央视台球': ['央视台球', 'CCTV央视台球', 'CCTV台球'],
    'CCTV-央视文化精品': ['央视文化精品', 'CCTV央视文化精品', '文化精品'],
    'CCTV-女性时尚': ['女性时尚', 'CCTV女性时尚', '央视女性时尚'],
    'CCTV-怀旧剧场': ['怀旧剧场', 'CCTV怀旧剧场', '央视怀旧剧场'],
    'CCTV-新科动漫': ['新科动漫', 'CCTV新科动漫', '央视新科动漫'],
    'CCTV-电视指南': ['电视指南', 'CCTV电视指南', '央视电视指南'],
    'CCTV-第一剧场': ['第一剧场', 'CCTV第一剧场', '央视第一剧场'],
    'CCTV-老故事': ['老故事', 'CCTV老故事', '央视老故事'],
    'CCTV-风云剧场': ['风云剧场', 'CCTV风云剧场', '央视风云剧场'],
    'CCTV-风云足球': ['风云足球', 'CCTV风云足球', '央视风云足球'],
    'CCTV-风云音乐': ['风云音乐', 'CCTV风云音乐', '央视风云音乐'],
    'CCTV-高尔夫网球': ['高尔夫网球', 'CCTV高尔夫网球', '央视高尔夫网球'],
    # CGTN 那一家子
    'CGTN': ['中国国际电视台', 'CCTV英语频道', 'CGTN英语'],
    'CGTN纪录': ['CGTN纪录片', 'CGTN Documentary'],
    'CGTN俄语': ['CGTN俄语频道', 'CGTN Russian'],
    'CGTN法语': ['CGTN法语频道', 'CGTN French'],
    'CGTN西语': ['CGTN西语频道', 'CGTN西班牙语'],
    'CGTN阿语': ['CGTN阿语频道', 'CGTN阿拉伯语'],
    # 卫视
    '东方卫视': ['上海卫视', '上海东方卫视', '东方卫视高清'],
    '湖南卫视': ['芒果台', '湖南卫视高清'],
    '浙江卫视': ['蓝莓台', '浙江卫视高清'],
    '江苏卫视': ['江苏卫视高清', '荔枝台'],
    '安徽卫视': ['安徽卫视高清', '海豚台'],
    '湖北卫视': ['湖北卫视高清'],
    '河南卫视': ['河南卫视高清', '大象台'],
    '北京卫视': ['北京卫视高清', 'BTV北京'],
    '深圳卫视': ['深圳卫视高清'],
    '广东卫视': ['广东卫视高清'],
    '山东卫视': ['山东卫视高清'],
    '四川卫视': ['四川卫视高清'],
    '辽宁卫视': ['辽宁卫视高清'],
    '黑龙江卫视': ['黑龙江卫视高清'],
    '天津卫视': ['天津卫视高清'],
    '重庆卫视': ['重庆卫视高清'],
    '江西卫视': ['江西卫视高清'],
    '福建卫视': ['东南卫视', '福建东南卫视'],
    '贵州卫视': ['贵州卫视高清'],
    '云南卫视': ['云南卫视高清'],
    '河北卫视': ['河北卫视高清'],
    '山西卫视': ['山西卫视高清'],
    '陕西卫视': ['陕西卫视高清'],
    '甘肃卫视': ['甘肃卫视高清'],
    '青海卫视': ['青海卫视高清'],
    '吉林卫视': ['吉林卫视高清'],
    '内蒙古卫视': ['内蒙古卫视高清'],
    '宁夏卫视': ['宁夏卫视高清'],
    '新疆卫视': ['新疆卫视高清'],
    '西藏卫视': ['西藏卫视高清'],
    '广西卫视': ['广西卫视高清'],
    '海南卫视': ['海南卫视高清', '旅游卫视'],
    # 平台自办
    '华数': ['华数TV', '华数频道', '华数TV频道'],
    '咪咕': ['咪咕视频', '咪咕体育', '咪咕直播'],
    'BesTV': ['百视通', 'BesTV百视通'],
    'NewTV': ['NewTV未来电视', '未来电视'],
    'CIBN': ['CIBN互联网电视', '国广东方'],
    # 港台
    '翡翠台': ['TVB翡翠台', '翡翠', '翡翠台高清', 'TVB翡翠台HD'],
    '明珠台': ['TVB明珠台', '明珠', '明珠台高清'],
    '無綫新聞台': ['无线新闻台', 'TVB新闻台', '無線新聞台', '翡翠新闻台', 'TVB无线新闻台'],
    '無綫財經體育資訊台': ['无线财经体育资讯台', '無線財經資訊台', 'TVB财经资讯台'],
    '有線新聞台': ['有线新闻台', '有线新闻', '有線新聞'],
    '香港開電視': ['香港开电视', '開電視', '奇妙电视'],
    '香港衛視': ['香港卫视', '香港卫视中文台'],
    '鳳凰衛視中文台': ['凤凰卫视中文台', '凤凰中文台', '凤凰中文', '凤凰卫视'],
    '鳳凰衛視資訊台': ['凤凰卫视资讯台', '凤凰资讯台', '凤凰资讯'],
    '鳳凰衛視香港台': ['凤凰卫视香港台', '凤凰香港台', '凤凰香港'],
    '澳視澳門': ['澳视澳门', '澳门电视台', '澳门澳视'],
    '澳視葡文': ['澳视葡文', '澳门葡文台'],
    '中天綜合台': ['中天综合台', '中天综合', '中天綜合'],
    '中天新聞台': ['中天新闻台', '中天新闻'],
    '東森戲劇': ['東森戲劇台', '东森戏剧台', '东森戏剧'],
    '東森電影': ['東森電影台', '东森电影台', '东森电影'],
    '東森新聞': ['東森新聞台', '东森新闻台', '东森新闻'],
    '八大第一台': ['八大第一', 'GTV第一台', '八大綜合台'],
    '纬来体育': ['緯來體育', '纬来体育台', '緯來體育台'],
    '智林體育台': ['智林体育台', '智林体育'],
    '好消息': ['好消息卫视', '好消息电视台', 'GOOD TV', '好消息衛星電視台'],
    '求索动物': ['求索动物频道', '动物星球'],
    '求索生活': ['求索生活频道'],
    '求索科学': ['求索科学频道'],
    '求索纪录': ['求索纪录频道'],
    # 日语
    'BSアニマックス ANIMAX': ['BSアニマックス', 'アニマックス', 'ANIMAX'],
    'BSテレ東': ['BSテレビ東京', 'BS TV TOKYO', 'BS东京'],
    'NHK G': ['NHK-G', 'NHK総合', 'NHK综合', 'NHK G 総合'],
    'NHK BS1': ['NHK-BS1', 'NHK BS 1'],
    'NHK WORLD JAPAN': ['NHK WORLD', 'NHK World Japan'],
    'テレビ朝日 tv asahi': ['テレビ朝日', 'tv asahi', '朝日电视台'],
    'テレビ東京 TV TOKYO': ['テレビ東京', 'TV TOKYO', '东京电视台'],
    'フジテレビ': ['フジテレビジョン', '富士电视台', 'Fuji TV'],
    '日本テレビ': ['日テレ', '日本电视台', 'NTV'],
    'TOKYO MX': ['TOKYO MX1', 'MXテレビ', '东京MX'],
    'J SPORTS 1': ['JSPORTS1', 'J SPORTS 1 HD'],
    'J SPORTS 2': ['JSPORTS2', 'J SPORTS 2 HD'],
    'J SPORTS 3': ['JSPORTS3', 'J SPORTS 3 HD'],
    'J SPORTS 4': ['JSPORTS4', 'J SPORTS 4 HD'],
    'MUSIC ON! TV': ['MUSIC ON TV', 'MUSIC ON!TV'],
    '日テレNEWS24 HD': ['日テレNEWS24', '日テレニュース24'],
    'スーパー!ドラマTV Super drama TV': ['スーパー!ドラマTV', 'Super drama TV'],
    'ディズニーch DISNEY CHANNEL JAPAN': ['ディズニーチャンネル', 'DISNEY CHANNEL JAPAN'],
    'ムービープラス Movie Plus': ['ムービープラス', 'Movie Plus'],
    'スターチャンネル1  STAR 1': ['スターチャンネル1', 'STAR 1', 'スターチャンネル 1'],
    'スターチャンネル2 STAR 2': ['スターチャンネル2', 'STAR 2'],
    'スターチャンネル3 STAR 3': ['スターチャンネル3', 'STAR 3'],
    'キッズステーション KIDS STATION': ['キッズステーション', 'KIDS STATION'],
    'ゴルフネットワーク GOLF NETWORK': ['ゴルフネットワーク', 'GOLF NETWORK'],
    'チャンネル NECO': ['チャンネルNECO', 'NECOチャンネル'],
    'ファミリー劇場': ['ファミリー劇場HD'],
    '日本映画専門チャンネル': ['日本映画専門チャンネルHD', '日本电影专门频道'],
    # 韩语
    'KBS WORLD': ['KBS World', 'KBS 월드'],
    'Arirang Korea': ['Arirang TV', '아리랑TV', '阿里郎电视台'],
    'Korea TV': ['KoreaTV', '코리아TV'],
    'LaLa TV': ['LaLaTV', 'ララTV'],
    # 内蒙古台：公开列表里一律用短名（`内蒙新闻`、`蒙语卫视`），索引里存的是全称。
    '内蒙古新闻综合': ['内蒙新闻'],
    '内蒙古经济生活': ['内蒙经济'],
    '内蒙古少儿': ['内蒙少儿'],
    '内蒙古文体娱乐': ['内蒙文体'],
    '内蒙古农牧': ['内蒙农牧'],
    '内蒙古蒙语卫视': ['蒙语卫视', '内蒙古蒙语'],
    '内蒙古蒙语文化': ['蒙语文化'],
    '巴彦淖尔新闻综合': ['巴彦淖尔新闻'],
    # 香港电台：iptv-org 写成 `RTHK TV 34 (港台電視34)`，中英两半都要认。
    # 31/32 早先就有一条 `RTHKTV31`（拉丁名）在索引里，那一半由它兜；33 的中文名
    # 还没人认领，挂在它身上（34/35 见下面的手挑条目）。
    'RTHKTV33': ['港台电视33', '港台33'],
    'HOY_TV': ['HOY 77', 'HOY77', '奇妙电视'],
    'HOY_资讯台': ['HOY 78', 'HOY78', 'HOY资讯'],
    # iptv-org 的英文名。这些台在中文列表里本来就能对上，挂英文名是让英文播放列表
    # 也能用上同一套图 —— 频道名是拉丁字母时，前面几级规则一点办法都没有。
    '安徽卫视': ['Anhui TV'],
    '河北卫视': ['Hebei TV'],
    '深圳卫视': ['Shenzhen Satellite TV'],
    '浙江国际': ['Zhejiang TV International'],
    '内蒙古卫视': ['Nei Monggol TV', 'Inner Mongolia TV'],
    '西藏卫视': ['Xizang TV', 'Xizang TV Tibetan'],
    '福建综合': ['Fujian Comprehensive Channel'],
    '江苏公共新闻': ['Jiangsu Public & News Channel'],
    '广西综艺旅游': ['Guangxi Variety & Travel Channel'],
    '金鹰卡通': ['Golden Eagle Cartoon'],
    '卡酷少儿': ['BRTV Kaku Childrens Channel', 'Kaku Childrens Channel'],
    '赤峰新闻综合': ['Chifeng Comprehensive News Channel', 'Chifeng Comprehensive News Chanel'],
    '安顺新闻综合': ['Anshun Comprehensive News Channel'],
    '通化综合': ['Tonghua TV'],
    '佛山综合': ['Foshan News TV'],
    '广州综合': ['Guangzhou TV'],
    '哈尔滨新闻综合': ['Harbin Comprehensive News Channel'],
    '哈尔滨影视': ['Harbin Movie Channel'],
    '吉林市新闻综合': ['Jilin City Channel'],
    '江西少儿': ["Jiangxi Children's Channel"],
    '江西经济生活': ['Jiangxi Economy & Life Channel'],
}

# 手挑条目：源里有图、脚本自己却挑不出来的那几个台。
#
# 「挑不出来」有两类：一类是频道名本身就是拉丁字母缩写（ViuTV、tvN、KBS1），
# 前面几级规则无法把它和别的台联系起来；一类是只有 sparks 那个镜像里才有的台，
# 而 sparks 不是骨架源，不留神就会整棵树地被筛掉。写死在这儿，一目了然也能审。
EXTRA_ENTRIES = [
    {
        'file': '港台电视34.png',
        'names': ['港台电视34', '港台34', 'RTHK TV 34', 'RTHK34'],
        'source': ('sparkssssssssss/epg', 'logo/港台电视34.png'),
        '_how': '手挑', '_example': 'RTHK TV 34 (港台電視34)',
    },
    {
        'file': '港台电视35.png',
        'names': ['港台电视35', '港台35', 'RTHK TV 35', 'RTHK35'],
        'source': ('sparkssssssssss/epg', 'logo/港台电视35.png'),
        '_how': '手挑', '_example': 'RTHK TV 35 (港台電視35)',
    },
    {
        'file': 'HOY国际财经台.png',
        'names': ['HOY国际财经台', 'HOY国际财经', 'HOY IBC', 'HOY76', 'HOY 76',
                  'HOY International Business Channel'],
        'source': ('sparkssssssssss/epg', 'logo/HOY国际财经台.png'),
        '_how': '手挑', '_example': 'HOY International Business Channel',
    },
    {
        'file': '美亚电影台.png',
        'names': ['美亚电影台', '美亞電影台', '美亚电影', 'Mei Ah Movie Channel'],
        'source': ('sparkssssssssss/epg', 'logo/美亚电影台.png'),
        '_how': '手挑', '_example': 'Mei Ah Movie Channel',
    },
    {
        'file': 'TVB亚洲剧台.png',
        'names': ['TVB亚洲剧台', '亚洲剧台', 'TVB亚洲剧', 'Asian Drama'],
        'source': ('sparkssssssssss/epg', 'logo/TVB亚洲剧台.png'),
        '_how': '手挑', '_example': 'Asian Drama (1080p)',
    },
    {
        'file': 'tvN.png',
        'names': ['tvN', 'tvN Asia'],
        'source': ('fanmingming/live', 'tv/tvN.png'),
        '_how': '手挑', '_example': 'tvN Asia HD (720p)',
    },
    {
        'file': '华视.png',
        'names': ['华视', '華視', 'CTS', 'HUA-CHI CTS'],
        'source': ('fanmingming/live', 'tv/华视.png'),
        '_how': '手挑', '_example': 'CTS (HUA-CHI CTS) (1080p)',
    },
    {
        'file': '民视.png',
        'names': ['民视', '民視', 'FTV', 'Formosa TV'],
        'source': ('fanmingming/live', 'tv/民视.png'),
        '_how': '手挑', '_example': 'FTV (民視) (720p) [Not 24/7]',
    },
    {
        'file': '原住民族电视台.png',
        'names': ['原住民族电视台', '原住民族電視台', '原住民电视台', '原视',
                  'Indigenous TV', 'Taiwan Indigenous TV'],
        'source': ('sparkssssssssss/epg', 'logo/原住民族电视台.png'),
        '_how': '手挑', '_example': 'Taiwan Indigenous TV (720p)',
    },
    {
        'file': 'ViuTV.png',
        'names': ['ViuTV', 'ViuTV 6', 'ViuTVsix'],
        'source': ('fanmingming/live', 'tv/viutv.png'),
        '_how': '手挑', '_example': 'ViuTV',
    },
    {
        'file': 'KBS1.png',
        'names': ['KBS1', 'KBS 1', 'KBS1TV'],
        'source': ('sparkssssssssss/epg', 'logo/KBS1.png'),
        '_how': '手挑', '_example': 'KBS1',
    },
    {
        'file': 'SBS.png',
        'names': ['SBS', 'SBS TV'],
        'source': ('sparkssssssssss/epg', 'logo/SBS.png'),
        '_how': '手挑', '_example': 'SBS',
    },
    {
        'file': 'WOWOW.png',
        'names': ['WOWOW', 'WOWOWプライム', 'WOWOW Prime'],
        'source': ('sparkssssssssss/epg', 'logo/WOWOW.png'),
        '_how': '手挑', '_example': 'WOWOW',
    },
    {
        'file': '优购物.png',
        'names': ['优购物', '優購物'],
        'source': ('fanmingming/live', 'tv/优购物.png'),
        '_how': '手挑', '_example': '优购物',
    },
    # 地市台：只收 iptv-org 的 cn.m3u 里点过名的那些（那份列表是真实订阅源）。
    {
        'file': '吉林影视.png',
        'names': ['吉林影视', 'Jilin Movie Channel'],
        'source': ('taksssss/tv', 'icon/吉林影视.png'),
        '_how': '手挑', '_example': 'Jilin Movie Channel',
    },
    {
        'file': '吉林生活.png',
        'names': ['吉林生活', 'Jilin Lifestyle Channel'],
        'source': ('taksssss/tv', 'icon/吉林生活.png'),
        '_how': '手挑', '_example': 'Jilin Lifestyle Channel',
    },
    {
        'file': '吉林乡村.png',
        'names': ['吉林乡村', 'Jilin Rural Channel'],
        'source': ('taksssss/tv', 'icon/吉林乡村.png'),
        '_how': '手挑', '_example': 'Jilin Rural Channel',
    },
    {
        'file': '江西都市.png',
        'names': ['江西都市', 'Jiangxi City Channel'],
        'source': ('taksssss/tv', 'icon/江西都市.png'),
        '_how': '手挑', '_example': 'Jiangxi City Channel',
    },
    {
        'file': '江西影视.png',
        'names': ['江西影视', 'Jiangxi Movie Channel'],
        'source': ('sparkssssssssss/epg', 'logo/江西影视.png'),
        '_how': '手挑', '_example': 'Jiangxi Movie Channel',
    },
    {
        'file': '江西公共农业.png',
        'names': ['江西公共农业', '江西公共·农业', 'Jiangxi Public & Agriculture Channel'],
        'source': ('taksssss/tv', 'icon/江西公共农业.png'),
        '_how': '手挑', '_example': 'Jiangxi Public & Agriculture Channel',
    },
    {
        'file': '兰州新闻综合.png',
        'names': ['兰州新闻综合', '兰州新闻综合频道', 'Lanzhou Comprehensive News Channel'],
        'source': ('taksssss/tv', 'icon/兰州新闻综合.png'),
        '_how': '手挑', '_example': 'Lanzhou Comprehensive News Channel',
    },
    {
        'file': '南昌新闻综合.png',
        'names': ['南昌新闻综合', '南昌新闻综合频道', 'Nanchang News & Generalist Channel'],
        'source': ('taksssss/tv', 'icon/南昌新闻综合.png'),
        '_how': '手挑', '_example': 'Nanchang News & Generalist Channel',
    },
    {
        'file': '四平综合.png',
        'names': ['四平综合', 'Siping TV'],
        'source': ('sparkssssssssss/epg', 'logo/四平综合.png'),
        '_how': '手挑', '_example': 'Siping TV',
    },
    {
        'file': '楚雄新闻.png',
        'names': ['楚雄新闻', 'Chuxiong News Channel'],
        'source': ('taksssss/tv', 'icon/楚雄新闻.png'),
        '_how': '手挑', '_example': 'Chuxiong News Channel',
    },
    {
        'file': '韶关综合.png',
        'names': ['韶关综合', '韶关综合台'],
        'source': ('taksssss/tv', 'icon/韶关综合.png'),
        '_how': '手挑', '_example': '广东 Ⅰ 韶关综合台 (720p)',
    },
    {
        'file': '韶关公共.png',
        'names': ['韶关公共', '韶关公共台'],
        'source': ('taksssss/tv', 'icon/韶关公共.png'),
        '_how': '手挑', '_example': '广东 Ⅰ 韶关公共台 (720p)',
    },
]

# Windows / URL 里不好用的字符换成 '-'。空格留着（`NHK G.png` 这种名字已经公开过一版，
# 改了名字等于换个地址，客户端那张缓存图就白下了）。
BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


# ---------------------------------------------------------------------------
# 归一化：与 App 侧 `lib/data/live/tv_logo_index.dart` 逐字对应
# ---------------------------------------------------------------------------

# 归一化时丢掉的标点（App 侧的 _ignoredPunctuation）。`+` 与 `!` **不在**这里：
# `CCTV5+` 与 `CCTV5` 是两个台。
IGNORED_PUNCTUATION = set(
    '-—_·.。&'
    '()（）[]【】{}｛｝〔〕'
    '「」『』《》〈〉'
    '|｜‖¦'
    '*＊~～、,，;；:：'
    '/／\\＼'
)

# 字母式符号区里「本该在数学字母块里」的那几个（ℂ ℍ ℤ …）。
LETTERLIKE_SPECIALS = {
    0x2102: 0x63, 0x210B: 0x68, 0x210D: 0x68, 0x210E: 0x68, 0x2110: 0x69,
    0x2111: 0x69, 0x2112: 0x6C, 0x2115: 0x6E, 0x2119: 0x70, 0x211A: 0x71,
    0x211B: 0x72, 0x211C: 0x72, 0x211D: 0x72, 0x2124: 0x7A, 0x2128: 0x7A,
    0x212C: 0x62, 0x212F: 0x65, 0x2130: 0x65, 0x2131: 0x66, 0x2133: 0x6D,
    0x2134: 0x6F,
}

# 数学字母块：每块 52 个连着的位置，A-Z 接着 a-z（缺字是空位，不影响偏移）。
MATH_LETTER_BLOCKS = [
    0x1D400, 0x1D434, 0x1D468, 0x1D49C, 0x1D4D0, 0x1D504, 0x1D538,
    0x1D56C, 0x1D5A0, 0x1D5D4, 0x1D608, 0x1D63C, 0x1D670,
]

# 频道名末尾那些「不是台名的一部分」的尾巴。**顺序有讲究**：长的在前
# （`超高清` 先于 `高清`）。
QUALITY_SUFFIXES = [
    'hd高清', '超高清', '蓝光高清', '高清', '超清', '极清', '标清', '蓝光', '原盘',
    '原画', '臻彩', '流畅', '高清版', 'fhd', 'uhd', 'hd', 'sd',
    '1080p', '1080i', '720p', '576p', '480p', '4k', '8k', '2k', 'hevc', 'avc',
    'h265', 'h264', 'avs', 'hdr', 'sdr', '10bit', '8bit', '60fps', '50fps',
    '30fps', '25fps', '50帧', '60帧', '杜比',
    '国语', '粤语', '普通话', '原声', '双语',
    'ipv6', 'ipv4', '电信', '联通', '移动', '咪咕', '广电', '备用', '备份',
    'backup', 'bk',
    'not247', 'geoblocked',
]

NUMBERED_SUFFIX = re.compile(r'(线路|备用|备份|backup|bk|line)\d+$')

# 包含匹配时，表里的键至少要这么长。
MIN_CONTAIN_KEY_LENGTH = 2

# 包含匹配时，键至少要占频道名的这个比例。
#
# 「包含」是三级里最弱的一级，收进来的键迟早会去抢别人的名字：`赛事` 落在
# `CCTV-5+ 体育赛事` 里，可它同样落在任何一个体育频道里。要求它**占名字的一大半**，
# `湖南卫视`（占 `湖南卫视高清` 的三分之二）过得去，`赛事`（五分之一）过不去 ——
# 这条线正好把「同一个台的另一种写法」与「碰巧是其一段的通用词」分开。
MIN_CONTAIN_COVERAGE = 0.5


def _fold_math(code):
    if 0x1D7CE <= code <= 0x1D7FF:
        return 0x30 + (code - 0x1D7CE) % 10
    special = LETTERLIKE_SPECIALS.get(code)
    if special is not None:
        return special
    for base in MATH_LETTER_BLOCKS:
        offset = code - base
        if 0 <= offset < 52:
            return 0x41 + offset if offset < 26 else 0x61 + offset - 26
    return None


def load_t2s(path):
    """OpenCC 的 TSCharacters.txt → {码点: 简体字}（多值取第一个，只收单字）。

    键用码点：`str.translate` 吃的就是码点到字符串的表，一次过完比逐字查表快。
    """
    table = {}
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, _, values = line.partition('\t')
            value = values.split()[0] if values.strip() else ''
            if len(key) == 1 and len(value) == 1 and key != value:
                table.setdefault(ord(key), value)
    return table


_T2S = {}


def normalize(name):
    out = []
    for char in name.translate(_T2S):
        code = ord(char)
        folded = _fold_math(code)
        if folded is not None:
            code = folded
        if 0xFF01 <= code <= 0xFF5E:  # 全角 → 半角
            code -= 0xFEE0
        if code == 0x3000:  # 全角空格
            continue
        char = chr(code)
        if char.isspace() or char in IGNORED_PUNCTUATION:
            continue
        out.append(char.lower())
    return ''.join(out)


def strip_suffix(key):
    """剥掉一层尾巴；剥不动就原样返回。"""
    numbered = NUMBERED_SUFFIX.search(key)
    if numbered and numbered.start() > 0:
        return key[:numbered.start()]
    for suffix in QUALITY_SUFFIXES:
        if len(key) > len(suffix) and key.endswith(suffix):
            return key[:-len(suffix)]
    return key


def strip_chain(key):
    """一级一级剥到剥不动为止（App 侧是 while 循环，这里把过程都留着）。"""
    chain = [key]
    while True:
        nxt = strip_suffix(chain[-1])
        if nxt == chain[-1] or not nxt:
            return chain
        chain.append(nxt)


def clean_name(name):
    return BAD_CHARS.sub('-', re.sub(r'\s+', ' ', name).strip()).strip()


def stem(name):
    return clean_name(name[:-4] if name.lower().endswith('.png') else name)


# ---------------------------------------------------------------------------
# 匹配：与 App 侧 TvLogoMatcher 同一套三级
# ---------------------------------------------------------------------------

class Matcher:
    """建索引那一端的匹配器：只用来算覆盖率（App 侧那份才是线上跑的）。"""

    def __init__(self, entries):
        self.exact = {}
        for file, names in entries:
            for name in names:
                key = normalize(name)
                if key:
                    self.exact.setdefault(key, file)
        self.by_length = sorted(
            (item for item in self.exact.items()
             if len(item[0]) >= MIN_CONTAIN_KEY_LENGTH),
            key=lambda item: -len(item[0]),
        )

    def url_for(self, channel_name):
        key = normalize(channel_name)
        if not key:
            return None
        hit = self.exact.get(key)
        if hit:
            return hit
        while True:
            stripped = strip_suffix(key)
            if stripped == key or not stripped:
                break
            key = stripped
            hit = self.exact.get(key)
            if hit:
                return hit
        for candidate, url in self.by_length:
            if candidate in key:
                return url
        return None


# ---------------------------------------------------------------------------
# 网络：清单走 GitHub API、素材走 raw，都落在 build/.cache 里（幂等、可离线重跑）
# ---------------------------------------------------------------------------

def http_get(url, timeout=60, retries=5):
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'ani-v-tv-logos-build/1.0 (+https://github.com/thehyaline)',
                'Accept': '*/*',
            })
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as error:  # noqa: BLE001 —— 网络这块什么错都可能冒出来
            last = error
            time.sleep(1 + attempt * 2)
    raise RuntimeError(f'取不到 {url}：{last}')


def cache_path(*parts):
    path = CACHE.joinpath(*[clean_name(str(part)) for part in parts])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def tree_of(source, refresh=False):
    """一个源的 PNG 清单（GitHub 的 tree API，一次请求拿全）。"""
    path = CACHE / f'tree_{source["repo"].replace("/", "_")}.json'
    if refresh or not path.exists():
        path.write_bytes(http_get(TREE.format(repo=source['repo'])))
    paths = json.loads(path.read_text(encoding='utf-8')).get('tree', [])
    return [item['path'] for item in paths if item['path'].lower().endswith('.png')]


def blob_of(source, path, refresh=False):
    """一个素材文件（raw 地址；文件名里的中文要百分号编码，urlopen 自己会编码）。"""
    local = cache_path('src', source['repo'].replace('/', '_'),
                       *path.split('/'))
    if refresh or not local.exists() or local.stat().st_size == 0:
        url = RAW.format(repo=source['repo'],
                         path=urllib.parse.quote(path))
        local.write_bytes(http_get(url))
    return local.read_bytes()


def fetch_list(list_id, repo, path, refresh=False):
    local = CACHE / 'lists' / f'{list_id}.m3u'
    if refresh or not local.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(http_get(RAW.format(repo=repo,
                                              path=urllib.parse.quote(path))))
    return local.read_text(encoding='utf-8', errors='replace')


def prefetch_blobs(pairs, workers=8, refresh=False):
    """把还缺的素材一次抓下来。

    一条一条抓太慢：每个文件一次 TLS 握手，一分钟只能过一百来张，一千多张要等十几
    分钟。几张一起抓（[workers] 条连接）快得多。已经下过的不再动（缓存命中）。
    """
    todo = []
    for source, path in pairs:
        local = cache_path('src', source['repo'].replace('/', '_'),
                           *path.split('/'))
        if refresh or not local.exists() or local.stat().st_size == 0:
            todo.append((source, path))
    if not todo:
        return []
    print(f'  要下载 {len(todo)} 张（其余命中缓存）', flush=True)
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(blob_of, source, path, refresh): (source, path)
                   for source, path in todo}
        for done, future in enumerate(as_completed(futures), 1):
            source, path = futures[future]
            error = future.exception()
            if error is not None:
                failures.append((source['repo'], path, error))
            if done % 100 == 0 or done == len(todo):
                print(f'    下载 {done}/{len(todo)}（失败 {len(failures)}）', flush=True)
    return failures


def playlist_names(text):
    """一份 m3u 里的频道名。规则与 App 侧一致：最后一个逗号之后是名字，
    没写就退到 tvg-name。"""
    names = []
    for line in text.splitlines():
        line = line.strip().lstrip('\ufeff')
        if not line.lower().startswith('#extinf:'):
            continue
        body = line[len('#extinf:'):]
        comma = body.rfind(',')
        attrs = body if comma < 0 else body[:comma]
        name = '' if comma < 0 else body[comma + 1:].strip()
        if not name:
            match = re.search(r'tvg-name\s*=\s*"([^"]*)"', attrs, re.I)
            name = match.group(1).strip() if match else ''
        if name:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# 计划：谁进来、用哪个源的哪张图
# ---------------------------------------------------------------------------

def load_curated():
    """手选条目那份名单 —— **它就是输入**。

    手工加过的那几条（以及 x1ao4 那套挑过配色的 141 张）在这里原样进来，重跑脚本
    不会把它们冲掉。索引里的名字顺序也照抄，绝不重排。

    输入取 `build/curated.json`（第一次生成索引之前的那份 `index.json`，冻在仓库里），
    没有才退回读 `index.json`。为什么不直接读 `index.json`：那是**产物**，每跑一次
    都会被这次的结果盖掉，下一轮再拿它当输入，「这个台标是从哪个源来的」就丢了 ——
    所有条目都会退化成「原有」，SOURCES.md 也就没法署明出处。冻结一份，跑多少遍
    都一样。
    """
    return read_entries(CURATED if CURATED.exists() else INDEX)


def load_index():
    """产物那份索引 —— 覆盖率自检拿它当「改后」。

    跟 [load_curated] 是两份东西：那份是**输入**（永不改动的手选名单），这份是
    上一次跑出来的结果。
    """
    return read_entries(INDEX)


def read_entries(path):
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    return [{'file': entry['file'], 'names': list(entry['names'])}
            for entry in data.get('entries', [])]


def source_files(source):
    """一个源里可用的文件 → {归一化键: 相对路径}。

    同一个键有几张图时挑一张：先挑文件名最干净的（名字归一化之后就等于键，
    没有 `(1)`、`+高清` 这类尾巴），再挑名字短的，最后按路径定序 —— 只为了
    每次跑出来的结果一样。
    """
    prefix = source['prefix']
    dirs = source['dirs']
    by_key = {}
    for path in sorted(tree_of(source)):
        if prefix and not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        directory = relative.rsplit('/', 1)[0] if '/' in relative else ''
        if dirs is not None and directory not in dirs:
            continue
        key = normalize(stem(relative.rsplit('/', 1)[-1]))
        if len(key) < MIN_CONTAIN_KEY_LENGTH or key in JUNK_KEYS:
            continue
        if key in QUALITY_SUFFIXES:  # `8K.png`、`4K电影.png` 这类不是台名
            continue
        rank = (0 if key == normalize(relative.rsplit('/', 1)[-1]) else 1,
                len(relative), relative)
        if key not in by_key or rank < by_key[key][0]:
            by_key[key] = (rank, path)
    return {key: value[1] for key, value in by_key.items()}


def has_wide_char(key):
    """键里有汉字或假名 —— 这类字符自成一体，认起来比拉丁字母准得多。"""
    return any('\u4e00' <= char <= '\u9fff' or '\u3040' <= char <= '\u30ff'
               for char in key)


def is_distinctive(key):
    """这个键够不够「像个台名」，够不够格参与**包含匹配**。

    包含匹配是「表里的键落在频道名里」，对中文台名是主力（`湖南卫视` ⊂ `湖南卫视高清`），
    对短的英文缩写却是个陷阱：`rt` 会落在 `Cartoon Network` 里，`ann` 会落在
    `Channel` 里，`24` 会落在 `CCTV-24` 里 —— 2~5 个字母的缩写碰巧出现在别人名字里
    太正常了，一旦收进索引就会去抢别人的台标。

    所以：中文键照旧（两个汉字已经很有辨识度），纯 ASCII 的键要求长一点，
    带数字的（`cctv1`、`kbs1` 这种台号）可以短一档。宁可不匹配，不可匹配错。
    """
    if key in JUNK_KEYS or key in QUALITY_SUFFIXES:
        return False
    if has_wide_char(key):
        return len(key) >= 2
    if len(key) >= 6:
        return True
    return len(key) >= 4 and any(char.isdigit() for char in key)


def is_backbone_ok(key):
    """这个键够不够格「没人用过也收进来」。

    除了公开列表点名的那些台，库里还会**背板式**地收下几个大源里名字像样的台
    （`新疆卫视`、`佛山公共`、`无锡新闻综合`……）：用户的播放列表是自己拼的，
    里面常出现公开列表没有的地方台，这些台在源里明明有图。

    只收**中文/日文的台名，四个字起**：短于四个字的（`戏曲`、`综艺`）太像通用词，
    留着只会去抢别人的台标。拉丁名一律不收 —— 源里带拉丁名的文件一半是拼音缩写
    （`youxifengyun`、`elta_yingju`）或内部编号（`1855DA`），收了也匹配不上谁；
    真有人拿英文名播这台（`Bloomberg TV`），那一支由公开列表点名带进来
    （见 [needed_keys]）。
    """
    if key in JUNK_KEYS or key in QUALITY_SUFFIXES:
        return False
    return has_wide_char(key) and len(key) >= 4


def needed_keys(names, available):
    """公开列表里出现过的频道 → 库里该收哪些键。

    判据不是「名字一样」，而是「App 拿这个名字去查，会不会查到」。所以照抄匹配器的
    三步，把每一步能命中的键都收进来：

    * **精确**：名字（或它剥掉后缀之后的某一级）写的就是这个键，多短都收；
    * **包含**：键是**剥到底**那个名字的**开头一段**（不能是中间或结尾的一段：那样
      收进来的多半是 `China Travel` 里的 `travel`，而不是同一个台的另一种写法）。
      这一级是猜出来的，还要过两道筛子（[is_distinctive] 与 [MIN_CONTAIN_COVERAGE]）。
      必须用「剥到底」的名字，也是跟 App 一样：`CCTV-3 (720p)` 剥成 `cctv3`，
      `cctv37` 只是跨过了 `cctv3` 与 `720p` 的接缝，App 永远匹配不到它。
    """
    wanted = {}
    for name in names:
        chain = strip_chain(normalize(name))
        for key in chain:
            if key in available:
                wanted.setdefault(key, (name, '精确'))
        final = chain[-1]
        for end in range(MIN_CONTAIN_KEY_LENGTH, len(final) + 1):
            sub = final[:end]
            if sub in wanted or sub not in available:
                continue
            if len(sub) < len(final) * MIN_CONTAIN_COVERAGE:
                continue
            if is_distinctive(sub):
                wanted[sub] = (name, '包含')
    return wanted


def attach_aliases(entry, aliases, owners, dropped):
    """按文件名给一个条目挂上别名。

    [owners] 是「名字键 → 文件名」的总表，一个键只能有一个主人（App 那边先到先得，
    两个台标抢同一个键等于随机）。撞上了就把这条别名丢掉。
    """
    for alias in aliases.get(stem(entry['file']), []):
        key = normalize(alias)
        if not key:
            continue
        owner = owners.get(key)
        if owner == entry['file']:
            continue
        if owner is not None:
            dropped.append((alias, entry['file'], owner))
            continue
        entry['names'].append(alias)
        owners[key] = entry['file']


def build_plan(curated, names, aliases):
    """算出最终要哪些条目、各自的图从哪来。"""
    entries = [dict(entry, source=None) for entry in curated]
    owners = {}
    dropped_aliases = []
    for entry in entries:
        for name in entry['names']:
            key = normalize(name)
            if key:
                owners.setdefault(key, entry['file'])
    # 别名先给**原有条目**挂上：下面挑新条目时要拿这张表挡重复
    # （别名占住的键，新条目不能再抢一次）。
    for entry in entries:
        attach_aliases(entry, aliases, owners, dropped_aliases)

    # 手挑条目：排在原有条目之后、自动挑选之前，占住的键同样挡后面的。
    for pin in EXTRA_ENTRIES:
        repo, path = pin['source']
        entry = {
            'file': pin['file'],
            'names': list(pin['names']),
            'source': {'repo': repo, 'path': path},
            '_example': pin.get('_example', pin['file']),
            '_how': pin.get('_how', '手挑'),
        }
        entries.append(entry)
        for name in entry['names']:
            key = normalize(name)
            if key:
                owners.setdefault(key, entry['file'])
    if EXTRA_ENTRIES:
        print(f'  手挑条目 {len(EXTRA_ENTRIES)} 个')

    layers = []
    for source in SOURCES:
        files = source_files(source)
        layers.append((source, files))
        print(f'  源 {source["id"]:<12} {len(files):>5} 个名字键')

    available = {}
    for source, files in layers:
        for key in files:
            available.setdefault(key, source['id'])

    wanted = needed_keys(names, available)
    print(f'  公开列表里的频道名 {len(names)} 条 → 该收的名字键 {len(wanted)} 个')

    # 背板：公开列表没点名、但源里名字像样的那些台（见 [is_backbone_ok]）。
    backbone_available = {key for source, files in layers
                          if source['id'] in BACKBONE_SOURCES
                          for key in files}
    backbone = 0
    for key in sorted(backbone_available):
        if key in wanted or key in owners or not is_backbone_ok(key):
            continue
        wanted[key] = (key, '背板')
        backbone += 1
    print(f'  再加上背板的 {backbone} 个 → 合计要收 {len(wanted)} 个键')

    used_names = {entry['file'] for entry in entries}
    added = 0
    for key, (example, how) in sorted(wanted.items(), key=lambda item: item[1][0]):
        if key in owners:
            continue
        picked = None
        for source, files in layers:
            if key in files:
                picked = (source, files[key])
                break
        if picked is None:
            continue
        source, path = picked
        base = stem(path.rsplit('/', 1)[-1])
        file = base + '.png'
        suffix = 1
        while file in used_names:
            suffix += 1
            file = f'{base}-{suffix}.png'
        used_names.add(file)
        entry = {'file': file, 'names': [base],
                 'source': {'repo': source['repo'], 'path': path},
                 '_example': example, '_how': how}
        base_key = normalize(base)
        if base_key:
            owners.setdefault(base_key, file)
        attach_aliases(entry, aliases, owners, dropped_aliases)
        entries.append(entry)
        added += 1
    print(f'  原有条目 {len(curated)} 个 + 手挑 {len(EXTRA_ENTRIES)} 个 '
          f'→ 加上新收的 {added} 个 → 共 {len(entries)} 个')
    for alias, owner, other in dropped_aliases[:10]:
        print(f'    （别名 {alias!r} 与 {other} 撞键，从 {owner} 上略过）')
    return entries


def verify_keys(entries):
    """两个台标抢同一个名字键时必须报出来：App 那边先到先得，等于随机。"""
    owners = {}
    conflicts = []
    for entry in entries:
        for name in entry['names']:
            key = normalize(name)
            if not key:
                continue
            if key in owners and owners[key] != entry['file']:
                conflicts.append((key, owners[key], entry['file']))
            owners.setdefault(key, entry['file'])
    return owners, conflicts


# ---------------------------------------------------------------------------
# 压缩
# ---------------------------------------------------------------------------

def encode_png(data):
    """压成宽度 ≤ MAX_WIDTH 的 PNG。

    两版都试一次（调色板 256 色 / 直接 RGBA），取小的那个：大多数台标是纯色块，
    量化能省一半；渐变多的（`CNA.png` 那种上万色的）量化反而更大，那就直存。
    """
    image = Image.open(io.BytesIO(data))
    image.load()
    if image.width > MAX_WIDTH:
        height = max(1, round(image.height * MAX_WIDTH / image.width))
        image = image.resize((MAX_WIDTH, height), Image.LANCZOS)
    rgba = image.convert('RGBA')

    buf = io.BytesIO()
    rgba.save(buf, 'PNG', optimize=True)
    best = buf.getvalue()

    try:
        quantized = rgba.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        buf = io.BytesIO()
        quantized.save(buf, 'PNG', optimize=True)
        if len(buf.getvalue()) < len(best):
            best = buf.getvalue()
    except Exception:  # 量化失败就用直存那一版，不值得为体积把一张图整个丢掉
        pass
    return best


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def load_tables(refresh=False):
    path = CACHE / 'TSCharacters.txt'
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(http_get(OPENCC_TS))
    _T2S.clear()
    _T2S.update(load_t2s(path))


def reference_names(refresh=False):
    names = []
    for list_id, repo, path, label in REFERENCE_LISTS:
        names.extend(playlist_names(fetch_list(list_id, repo, path, refresh)))
    return names


def cmd_plan(args):
    load_tables(args.refresh)
    curated = load_curated()
    names = reference_names(args.refresh)
    print(f'公开列表合计 {len(names)} 条频道名')
    entries = build_plan(curated, names, ALIASES)
    owners, conflicts = verify_keys(entries)
    print(f'名字键 {len(owners)} 个，冲突 {len(conflicts)} 处')
    for key, first, second in conflicts[:20]:
        print(f'  ⚠ {key!r}：{first} 与 {second}')
    new = [entry for entry in entries if entry.get('source')]
    print(f'新增条目 {len(new)} 个，前 {args.verbose or 20} 个：')
    for entry in new[:args.verbose or 20]:
        print(f'  {entry["file"]:<28} ← {entry["source"]["repo"]}/{entry["source"]["path"]}'
              f'   （{entry["_how"]}：{entry["_example"]}）')
    by_source = {}
    for entry in entries:
        source = entry['source']['repo'] if entry.get('source') else '(原有)'
        by_source[source] = by_source.get(source, 0) + 1
    print('来源分布：', by_source)


def cmd_build(args):
    load_tables(args.refresh)
    curated = load_curated()
    names = reference_names(args.refresh)
    print(f'公开列表合计 {len(names)} 条频道名')
    entries = build_plan(curated, names, ALIASES)

    owners, conflicts = verify_keys(entries)
    if conflicts:
        print(f'⚠ 名字键冲突 {len(conflicts)} 处（先到先得，第二个台标永远匹配不到）')
        for key, first, second in conflicts[:20]:
            print(f'    {key!r}：{first} 与 {second}')

    # 原有条目要写清「当初是 x1ao4 里的哪一张」：按文件名倒查一遍那个源的清单。
    x1ao4_files = source_files(SOURCES[0])

    # 先把要下载的（新增条目的）素材并行抓下来，缺哪张再补哪张。
    by_repo = {source['repo']: source for source in SOURCES}
    failures = prefetch_blobs(
        [(by_repo[entry['source']['repo']], entry['source']['path'])
         for entry in entries if entry.get('source')],
        refresh=args.refresh,
    )
    for repo, path, error in failures:
        print(f'  ⚠ 下载失败 {repo}/{path}：{error}')

    LOGOS.mkdir(exist_ok=True)
    manifest = {}
    total = 0
    for position, entry in enumerate(entries, 1):
        file = entry['file']
        local = LOGOS / file
        source = entry.get('source')
        if source is None:
            # 原有条目：图已经在 logos/ 里，只在超宽时重压一次（宽度上限是全库统一的）。
            data = local.read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                width = image.width
            if width > MAX_WIDTH:
                data = encode_png(data)
                local.write_bytes(data)
            origin = {
                'repo': 'x1ao4/tv-logos',
                'path': x1ao4_files.get(normalize(stem(file)),
                                        f'(原有) {file}'),
            }
        else:
            source_def = next(item for item in SOURCES
                              if item['repo'] == source['repo'])
            raw = blob_of(source_def, source['path'], args.refresh)
            data = encode_png(raw)
            local.write_bytes(data)
            origin = source
        total += len(data)
        manifest[file] = {
            'source': origin['repo'],
            'path': origin['path'],
            'sha256': sha256(data),
            'bytes': len(data),
            'names': entry['names'],
        }
        if position % 100 == 0 or position == len(entries):
            print(f'  {position}/{len(entries)} … {total / 1024 / 1024:.1f} MB', flush=True)

    manifest['_generated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                        encoding='utf-8')

    index = {
        'name': 'ani-v-tv-logos',
        'version': 2,
        'updated': time.strftime('%Y-%m-%d'),
        'logoBase': ('https://raw.githubusercontent.com/thehyaline/'
                     'ani-v-tv-logos/main/logos/'),
        'entries': [{'file': entry['file'], 'names': entry['names']}
                    for entry in entries],
    }
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1) + '\n',
                     encoding='utf-8')
    write_sources_md(entries)

    # 上一轮收进来、这一轮不要了的图得删掉：留着就是没人引用的死图，
    # 白占体积，还容易让人以为它还在用。
    live = {entry['file'] for entry in entries}
    orphans = sorted(path.name for path in LOGOS.glob('*.png')
                     if path.name not in live)
    for name in orphans:
        (LOGOS / name).unlink()
    if orphans:
        print(f'  清掉 {len(orphans)} 张没人引用的旧图：'
              + '、'.join(orphans[:5]) + ('…' if len(orphans) > 5 else ''))

    print(f'条目 {len(entries)} 个，名字键 {len(owners)} 个')
    print(f'图标合计 {total / 1024 / 1024:.2f} MB')


def origin_repo(entry):
    """一个条目的来源仓库。

    手选那一批（最初的 141 张）在 index.json 里没有 `source` 字段 —— 它们都是
    x1ao4 那套里挑出来的，所以照 x1ao4 记。"""
    origin = entry.get('source')
    return origin['repo'] if origin else 'x1ao4/tv-logos'


def write_sources_md(entries):
    lines = [
        '# 台标来源',
        '',
        '本库的图**没有一张是自己画的**，全部取自下面这些公开仓库，只做了挑图、改名、',
        '压缩、按频道名建索引这四件事（脚本见 `build/build_logos.py`）。',
        '台标版权归各电视台所有，此处仅作识别之用；**仅供个人学习与自用，请勿商用**。',
        '权利人如果认为这里不该有某张图，提个 Issue，我们立刻删。',
        '',
        '## 分来源',
        '',
        '| 来源 | 用了几张 | 说明 |',
        '|---|---|---|',
    ]
    for source in SOURCES:
        count = sum(1 for entry in entries if origin_repo(entry) == source['repo'])
        lines.append(f'| {source["credit"]} | {count} | {source["note"]} |')
    lines += [
        '',
        '## 逐文件',
        '',
        '| 图标 | 来源 | 源文件 |',
        '|---|---|---|',
    ]
    for entry in sorted(entries, key=lambda item: item['file']):
        origin = entry.get('source')
        repo = origin_repo(entry)
        path = origin['path'] if origin else '（手选那一批，见上）'
        lines.append(f'| `{entry["file"]}` | {repo} | `{path}` |')
    SOURCES_MD.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def cmd_check(args):
    """对公开播放列表算命中率：改前（git 基线）vs 改后（现在的 index.json）。"""
    load_tables(args.refresh)
    lines = []

    def say(line=''):
        print(line, flush=True)
        lines.append(line)

    current = load_index()
    baseline = None
    if args.baseline:
        try:
            text = subprocess.run(
                ['git', 'show', f'{args.baseline}:index.json'],
                cwd=ROOT, capture_output=True, check=True,
            ).stdout.decode('utf-8')
            baseline = [(entry['file'], entry['names'])
                        for entry in json.loads(text)['entries']]
        except Exception as error:
            say(f'（拿不到基线 {args.baseline}：{error}）')
    current_pairs = [(entry['file'], entry['names']) for entry in current]
    before = Matcher(baseline) if baseline else None
    after = Matcher(current_pairs)

    say('# 覆盖率：公开播放列表对台标库的命中率')
    say()
    say(f'* 改前：`git show {args.baseline}:index.json`，{len(before.exact) if before else 0} 个名字键')
    say(f'* 改后：`index.json`，{len(after.exact)} 个名字键')
    say()

    total_before = total_after = total_names = 0
    for list_id, repo, path, label in REFERENCE_LISTS:
        names = playlist_names(fetch_list(list_id, repo, path, args.refresh))
        unique = list(dict.fromkeys(names))
        total_names += len(unique)
        say(f'## {label}（{len(unique)} 个频道）')
        say()
        say('| | 命中 | 占比 |')
        say('| --- | --- | --- |')
        rows = []
        for name, matcher in (('改前', before), ('改后', after)):
            if matcher is None:
                continue
            hit = [n for n in unique if matcher.url_for(n)]
            rows.append((len(hit), matcher))
            if name == '改前':
                total_before += len(hit)
            else:
                total_after += len(hit)
            say(f'| {name} | {len(hit)}/{len(unique)} | {len(hit) * 100 // len(unique)}% |')
        missing = [n for n in unique if not after.url_for(n)]
        if missing:
            say()
            say(f'没配上图的 {len(missing)} 个：')
            say()
            for name in missing[:args.verbose or len(missing)]:
                say(f'* {name}')
        say()

    if before is not None:
        say('## 合计')
        say()
        say(f'* 改前：{total_before}/{total_names} = {total_before * 100 // total_names}%')
        say(f'* 改后：{total_after}/{total_names} = {total_after * 100 // total_names}%')

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'\n报告写到 {path}')


SELFTEST_CASES = [
    # (输入, 归一化结果) —— 与 App 侧 test/tv_logo_index_test.dart 同一批例子
    ('CCTV-1 综合', 'cctv1综合'),
    ('ＣＣＴＶ－１　综合', 'cctv1综合'),
    ('凤凰·中文', '凤凰中文'),
    ('CHC_动作电影', 'chc动作电影'),
    ('無綫翡翠台', '无线翡翠台'),
    ('八大戲劇', '八大戏剧'),
    ('臺視新聞', '台视新闻'),
    ('CCTV5+', 'cctv5+'),
    ('ＭＵＳＩＣ　ＯＮ！ ＴＶ', 'musicon!tv'),
    ('J SPORTS (1)', 'jsports1'),
    ('湖南卫视[1280*720]', '湖南卫视1280720'),
    ('湖南卫视「IPV6」', '湖南卫视ipv6'),
    ('广东 ‖ 清新综合台', '广东清新综合台'),
    ('CCTV-1 (720p) [Not 24/7]', 'cctv1720pnot247'),
    ('咪咕直播 𝟜𝕂-𝟙「移动」', '咪咕直播4k1移动'),
    ('𝐂𝐂𝐓𝐕𝟙', 'cctv1'),
    ('𝕂𝔹𝕊', 'kbs'),
    ('ℂℕℕ', 'cnn'),
    ('【】（）', ''),
]


def cmd_selftest(args):
    load_tables(args.refresh)
    bad = 0
    for text, want in SELFTEST_CASES:
        got = normalize(text)
        if got != want:
            bad += 1
            print(f'✗ {text!r} → {got!r}（应为 {want!r}）')
    chain = strip_chain(normalize('CCTV-1 综合 高清 HD'))
    if chain != ['cctv1综合高清hd', 'cctv1综合高清', 'cctv1综合']:
        bad += 1
        print(f'✗ 剥后缀链不对：{chain}')
    matcher = Matcher([
        ('CCTV1.png', ['CCTV1', 'CCTV-1 综合', '央视一套']),
        ('CCTV13.png', ['CCTV13', 'CCTV-13 新闻']),
        ('NHK_BS1.png', ['NHK BS1']),
        ('翡翠台.png', ['翡翠台', 'TVB翡翠台', '無綫翡翠台']),
    ])
    match_cases = [
        ('CCTV13新闻', 'CCTV13.png'),
        ('CCTV-1综合高清', 'CCTV1.png'),
        ('無綫翡翠台', '翡翠台.png'),
        ('翡翠台 4K', '翡翠台.png'),
        ('BS1', None),
        ('某某县电视台', None),
    ]
    for text, want in match_cases:
        got = matcher.url_for(text)
        if got != want:
            bad += 1
            print(f'✗ 匹配 {text!r} → {got!r}（应为 {want!r}）')
    print('自检通过' if not bad else f'自检失败：{bad} 处')
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true', help='只算不写')
    mode.add_argument('--build', action='store_true', help='下载 + 压缩 + 写产物')
    mode.add_argument('--check', action='store_true', help='公开列表覆盖率')
    mode.add_argument('--selftest', action='store_true', help='规则自检')
    parser.add_argument('--refresh', action='store_true', help='忽略缓存重取')
    parser.add_argument('--baseline', default='HEAD', help='覆盖率对比的基线（git rev）')
    parser.add_argument('--verbose', type=int, default=0, help='列出没配上图的频道')
    parser.add_argument('--report', nargs='?', const=str(COVERAGE),
                        help='把覆盖率报告写到文件（默认 build/coverage.md）')
    args = parser.parse_args()
    if args.selftest:
        return cmd_selftest(args)
    if args.plan:
        return cmd_plan(args)
    if args.build:
        return cmd_build(args)
    return cmd_check(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
