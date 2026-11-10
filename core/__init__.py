# -*- coding: utf-8 -*-
"""分析核心。

★ 這個套件底下**絕不 import PySide6**。

理由：GitHub Pages 是純靜態的，跑不了 Python，所以 web 版的架構必須是
「GitHub Actions 跑 Python 輸出 JSON，網頁只負責顯示」。
從一開始就把邏輯和介面分開，階段 6 轉 web 時就不用重寫任何分析程式碼。

這是 1151022s → 1151022s-1 最大的痛點，這次從頭避開。
"""
