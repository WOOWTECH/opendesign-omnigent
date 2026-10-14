---
name: woowtech-brand
zh_name: "woowtech 品牌視覺"
en_name: "woowtech Brand"
emoji: "🔷"
description: "woowtech (渥屋科技) brand system — blue #6183FC spot colour, 20px radius, Poppins/Outfit/Yellowtail/Noto Sans TC, MDI icons in blue-050 tiles, frosted glass. No emoji."
zh_description: "woowtech 渥屋科技品牌系統 — #6183FC 點綴藍、20px 圓角、Poppins/Outfit/Yellowtail/Noto Sans TC、MDI 圖示藍底磚、霧面玻璃。禁用 emoji。"
en_description: "woowtech brand system: #6183FC spot blue, 20px radius, Poppins/Outfit, MDI icon tiles, frosted glass, no emoji."
category: brand
scenario: marketing
aspect_hint: "16:9 橫向 1920x1080"
featured: 1
recommended: 1
tags: ["woowtech", "渥屋科技", "brand", "aiot", "smart-space", "mdi"]
od:
  mode: deck
  surface: web
  scenario: marketing
  featured: 0.001
  preview:
    type: html
    entry: index.html
    reload: debounce-100
  design_system:
    requires: false
  example_prompt: "Use the woowtech brand system to build this. Blue #6183FC is the only spot colour at roughly 10% of the frame, backgrounds are white and #EFF1F5 grey, every card is 20px radius, icons are MDI glyphs in #EEF2FF tiles, and there is no emoji anywhere."
  example_prompt_i18n:
    zh-TW: "用 woowtech 品牌系統製作。藍色 #6183FC 是唯一點綴色且只佔約 10% 版面，背景是白色與 #EFF1F5 灰，所有卡片 20px 圓角，圖示用 MDI 放在 #EEF2FF 色磚裡，全篇不得出現 emoji。"
---

【woowtech · 渥屋科技 · WOOW Technology】AIoT / 智慧空間品牌
Tagline: The Smart Space Solution · 智慧空間解決方案
四支柱: Air · Mind · Community · Nourishment　室內助理: Albi

# 硬性規則 — MUST / NEVER，逐條照做

## 1. 色彩（比例是硬約束）
版面配色比例必須接近：**白 50% · 灰 20% · 深灰 10% · 藍 10% · 黑 5% · 其他 accent ≤5%**。
藍色是**唯一主色**，只做點綴，絕不可鋪滿大面積背景（整頁 section divider 例外）。

```css
:root{
  --ww-blue:#6183FC; --ww-blue-700:#3F63E8; --ww-blue-050:#EEF2FF;
  --ww-white:#FFFFFF; --ww-gray:#EFF1F5; --ww-deep-gray:#646262; --ww-black:#212121;
  --text-primary:#212121; --text-secondary:#646262;
  --surface-page:#EFF1F5; --surface-card:#FFFFFF;
}
```
Accent（各自 ≤5%，一個版面只挑一個）：
`#7BDBE0` `#F8D158` `#65C2E0` `#6791DE` `#8CD37F` `#B17148` `#F1C692` `#E66D3E` `#F45D6D` `#C09FE0`

## 2. 字型
```html
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&family=Outfit:wght@100;300;400;500;600&family=Yellowtail&family=Noto+Sans+TC:wght@400;500;700;900&display=swap">
```
```css
:root{
  --font-title:'Poppins','Noto Sans TC',sans-serif;
  --font-body:'Outfit','Noto Sans TC',sans-serif;
  --font-script:'Yellowtail',cursive;
}
```
- 標題 Poppins，字距 `-0.02em`，行高 1.05–1.12。
- 內文 Outfit，行高 1.6，字重輕。
- Yellowtail **只給一個溫暖的詞**（通常 accent 色），**絕不可用在內文**。
- 中文一律 Noto Sans TC。

## 3. 圓角、陰影、霧面玻璃
```css
:root{
  --space-4:16px; --space-6:24px; --space-8:32px; --space-12:48px; --section-y:96px;
  --radius-sm:12px; --radius:20px; --radius-lg:28px; --radius-pill:999px;
  --shadow-sm:0 2px 8px rgba(33,33,33,.06);
  --shadow-md:0 10px 28px rgba(33,33,33,.08);
  --shadow-lg:0 22px 52px rgba(33,33,33,.12);
  --shadow-float:0 26px 60px -18px rgba(97,131,252,.28);
  --glass-bg:rgba(255,255,255,.7); --glass-border:rgba(255,255,255,.9);
}
```
- **圓角只允許 12 / 20 / 28 / 999px 四種**。卡片與矩形一律 20px。按鈕、開關、頭像用 pill。
- 陰影一律柔和擴散、低透明度。**絕不可用硬黑陰影。**
- 霧面玻璃 `background:var(--glass-bg); backdrop-filter:blur(20px); border:1px solid var(--glass-border)`
  —— **只能疊在顏色或影像上**，不可疊在純白平面。

## 4. 圖示 — 只用 Material Design Icons（MDI）
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css">
```
標準圖示磚（照抄）：
```html
<span style="display:inline-flex;width:48px;height:48px;border-radius:16px;
  background:var(--ww-blue-050);align-items:center;justify-content:center">
  <i class="mdi mdi-air-filter" style="font-size:26px;color:var(--ww-blue)"></i>
</span>
```
常用 slug：
- Air `mdi-weather-windy` `mdi-air-filter` `mdi-air-purifier` `mdi-fan`
- Mind `mdi-brain` `mdi-meditation` `mdi-heart-pulse`
- Community `mdi-account-group` `mdi-home-group` `mdi-handshake`
- Nourishment `mdi-leaf` `mdi-sprout` `mdi-recycle-variant`
- 智慧家庭 `mdi-home-automation` `mdi-lightbulb-on` `mdi-thermostat` `mdi-air-conditioner` `mdi-lock-smart` `mdi-motion-sensor`
- 感測 `mdi-gauge` `mdi-molecule-co2` `mdi-thermometer` `mdi-water-percent`
- Albi `mdi-robot-happy-outline` `mdi-message-processing-outline` `mdi-shimmer`
- 能源 `mdi-flash` `mdi-chart-line` `mdi-solar-power` `mdi-battery-charging`
- UI `mdi-check-circle` `mdi-circle-medium` `mdi-chevron-right` `mdi-cog-outline`

找不到就上 pictogrammers.com/library/mdi 挑最接近的細線幾何款（UI 優先 `-outline`）。
**絕不可換成別套圖示或用 emoji 補位。**

## 5. 版面 / 簡報配方
畫布 1920×1080（16:9）。內文 ≥24px，標題 60–112px。section padding 96px，內容最寬約 1200px。

1. **封面** — 灰或白底 · Poppins 大標 · 一個 Yellowtail accent 字 · 四支柱 MDI 磚列 · 左上 logo
2. **段落分隔** — 整頁 `#6183FC` 藍底 · 白色 Poppins 標題 · 一個超大 MDI 字符浮水印（白色 6% 透明）
3. **數據 KPI** — 浮動卡（必須帶 `--shadow-float`）· 巨大 Poppins 藍色數字 · MDI 磚 + 標籤
4. **產品特色** — 霧面玻璃卡疊在霧面影像上 · 每個特色一個 MDI 磚 · 全部 20px 圓角
5. **四支柱網格** — 四張卡，各 = MDI 磚 + 標題 + 一行說明 · 髮絲邊 `rgba(33,33,33,.08)`
6. **標語 / 使命** — 置中 Outfit 細體大字 · "Human-centered" 用 Yellowtail 橘色

背景只能是平灰、平白，或冷白藍色調的霧面影像。**不可用重漸層、不可預設深色模式、不可用雜亂材質。**

## 6. 語氣
溫暖、以人為本。對使用者稱 **"you"**；Albi 用第一人稱；公司用 "we"。
標題 Title Case；字標 **小寫 "woowtech"**；UI 標籤 sentence case。中英並置但視覺區隔。
關鍵詞：**human-centered**。**全篇禁用 emoji** —— 溫度來自色彩、MDI 圖示、手寫體與文案。

## 7. 交付前自我檢查（逐條核對再輸出）
- [ ] 藍色 ≤10% 版面？accent ≤5%？背景是白/灰？
- [ ] 所有卡片圓角 = 20px？沒有出現 12/20/28/999 以外的圓角？
- [ ] 圖示全是 `mdi-` 且放在 `--ww-blue-050` 色磚裡？沒有混用其他圖示？
- [ ] **整份文件 0 個 emoji？**
- [ ] Poppins 標題 + Outfit 內文 + Noto Sans TC 中文都有載入？
- [ ] Yellowtail 只出現在一個詞、且不在內文？
- [ ] 陰影都是柔和低透明度？沒有硬黑陰影？
- [ ] 沒有重漸層、沒有深色底？
- [ ] `:root` 裡宣告了上面的 CSS 變數？
