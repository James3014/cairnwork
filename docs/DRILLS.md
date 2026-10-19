# 演練紀錄

議會 H 條：裸骨幹必須通過正常、失敗、重啟、回退四種演練，才可以搬入任何實驗能力。這裡只寫實際跑出來的結果。

| 演練 | 狀態 | 證據 |
|---|---|---|
| 正常 | **通過**（2026-10-20，1 次） | 見下方 |
| 失敗 | 自動測試覆蓋：錯誤修正 → `CORE_FAILED_VERIFICATION`；改到範圍外的檔案 → `DIRTY_TREE`；沒有改動 → `NO_COMMIT`；worker 當掉 → `WORKER_FAILED`。四種結果的任務狀態都是 FAILED（`tests/test_pipeline.py`）。真 agent 的失敗演練還沒跑。 | `tests/test_pipeline.py::test_failures_never_complete` |
| 重啟 | **未做**：重啟恢復（保留行為 #21）還沒實作 | — |
| 回退 | **未做**：線上切換（議會 F 條）還沒開始 | — |

## 正常演練 2026-10-20

- 腳本：`uv run python scripts/drill_normal.py <空目錄> gemini-3.8-flash-high`
- 題目：`src/stats.py` 的 `average()` 多除了 1；`tests/test_stats.py` 有兩個測試會失敗。
- 允許修改的檔案：`src/stats.py`。verifier：`python3 -m pytest -q tests/test_stats.py`。
- worker：Agy `gemini-3.8-flash-high`，使用 `--mode accept-edits`（不能執行指令）。
- 結果：任務狀態 `COMPLETED`，Core 判定 `VERIFIED`，從派工到公證約 21 秒。
- agent 只改了一行：

```diff
-    return sum(values) / (len(values) + 1)
+    return sum(values) / len(values)
```

這只是一次、一個很簡單的題目，只能證明整條主線接通了，不能拿來說明 agent 的能力或成功率。
