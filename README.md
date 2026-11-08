# Tactica Universalis 地圖碰撞 harness

CSGO 電競俱樂部模擬遊戲 Tactica Universalis 的地圖碰撞設計工具。這不是遊戲本體。

它使用一份固定的地圖幾何資料，把牆面與可行走區域畫成俯視碰撞格，並寫出預覽，用來對齊遊戲裡的地圖。下載快取與預覽圖不放進這個倉庫。

遊戲本體：[TacticaUniversalis](https://github.com/MoYan1250/TacticaUniversalis)

## 怎麼放

這兩個腳本放在遊戲專案的 `Tools/map_collision`。遊戲根目錄是此資料夾往上兩層。碰撞快取寫到 `Tools/map_collision_cache`，預覽寫到 `Tools/map_collision_out`。

```bash
pip install -r requirements.txt
python bake_map_collision.py
```

## 授權

沒有開放原始碼授權。這是作品集裡的設計工具。地圖幾何與名稱屬於各自的權利人。
