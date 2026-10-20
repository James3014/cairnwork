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
- worker：Agy `gemini-3.8-flash-high`，使用 `--mode accept-edits`（當時尚未加 `--sandbox`）。
- 結果：任務狀態 `COMPLETED`，Core 判定 `VERIFIED`，從派工到公證約 21 秒。
- 可追查的編號：
  - task_id `task-68df22ce7ea74946a621e6b0c1c9ec4e`
  - attempt `a04f1167d47043793`
  - 候選 commit `5f64cac948b613b10aa8fe2ee9c6a4f1deac3ffc`（在演練目錄的 project repo 中，ref 為 `refs/cairnwork/attempts/a04f1167d47043793`）
  - verifier log 和 notary／ownership 資料庫留在本機的演練目錄；那是暫存區，不會進 repo。
- agent 只改了一行：

```diff
-    return sum(values) / (len(values) + 1)
+    return sum(values) / len(values)
```

這只是一次、一個很簡單的題目，只能證明整條主線接通了，不能拿來說明 agent 的能力或成功率。

## Agy worker 權限實測 2026-10-20

使用的參數和 worker 相同：`agy -p ... --model gemini-3.8-flash-high --mode accept-edits --sandbox`，在 headless print mode 下執行，提示詞明確要求模型去做下列的事。

| 要求 | 結果 |
|---|---|
| 執行 shell 指令（`touch <檔>`） | **被拒**：`a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied`，檔案沒有建立 |
| 在工作目錄外建立檔案（絕對路徑，以及 `../`） | **被拒**：`required the "write_file" permission ... auto-denied`，檔案沒有建立 |
| 修改工作目錄內的檔案 | 允許（見正常演練） |

注意事項：
- 被拒之後，agy 會中止整次執行，連允許的修改也不會套用，所以這次嘗試的結果是 `NO_COMMIT`。
- 這是 agy 自己的權限機制，不是作業系統層級的 sandbox，所以 Cairnwork 不把它當成安全邊界。真正的防線仍然是：只收允許的檔案，並比對整個 clone。
- 另一個測試：提示詞裡帶著 Cairnwork 的規則時，模型會直接拒絕執行這些要求。這只代表模型遵守了規則，不能證明權限有在擋。
